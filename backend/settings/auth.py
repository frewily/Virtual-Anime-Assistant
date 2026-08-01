"""In-memory authentication for the local settings interface."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import base64
import binascii
import hashlib
import hmac
import secrets
import threading
import time

from settings.models import AuthRecord


_ALGORITHM = "scrypt"
_SCRYPT_N = 32768
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 128 * 1024 * 1024
_SALT_BYTES = 16
_TOKEN_BYTES = 32
_SESSION_LIFETIME_SECONDS = 30 * 60
_FAILURE_WINDOW_SECONDS = 60
_MAX_FAILURES_PER_WINDOW = 5
_MAX_FAILURE_CLIENTS = 1024
_MAX_CLIENT_LENGTH = 256
_MAX_ACTIVE_SESSIONS = 1024
_TOKEN_GENERATION_ATTEMPTS = 8


class AuthError(Exception):
    """A sanitized authentication infrastructure failure."""


class PasswordPolicyError(AuthError):
    """Raised when a new password does not meet the fixed local policy."""


class LoginRateLimited(AuthError):
    """Raised when a client has too many recent failed logins."""


@dataclass(frozen=True)
class Session:
    """An absolute-expiry local settings session."""

    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: float


class SettingsAuthService:
    """Password verification, login throttling, and ephemeral sessions."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._clock = clock
        self._random_bytes = random_bytes
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._login_failures: dict[str, deque[float]] = {}

    def hash_password(self, password: str) -> AuthRecord:
        """Create a persisted scrypt record for a policy-compliant password."""
        encoded_password = self._encode_new_password(password)
        salt = self._secure_random(_SALT_BYTES)
        try:
            password_hash = hashlib.scrypt(
                encoded_password,
                salt=salt,
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                maxmem=_SCRYPT_MAXMEM,
                dklen=_SCRYPT_DKLEN,
            )
        except Exception:
            raise AuthError("authentication operation failed") from None

        return AuthRecord(
            algorithm=_ALGORITHM,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            salt=self._encode_base64(salt),
            hash=self._encode_base64(password_hash),
        )

    def verify_password(self, password: str, record: AuthRecord) -> bool:
        """Verify without trusting persisted algorithm or cost parameters."""
        if not self._record_has_allowed_parameters(record):
            return False

        try:
            encoded_password = self._encode_password(password)
            encoded_salt = self._preflight_base64(
                record.salt, expected_length=_SALT_BYTES
            )
            encoded_hash = self._preflight_base64(
                record.hash, expected_length=_SCRYPT_DKLEN
            )
            salt = self._decode_base64(
                encoded_salt, expected_length=_SALT_BYTES
            )
            expected_hash = self._decode_base64(
                encoded_hash, expected_length=_SCRYPT_DKLEN
            )
        except (AttributeError, TypeError, UnicodeError, ValueError):
            return False

        try:
            actual_hash = hashlib.scrypt(
                encoded_password,
                salt=salt,
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                maxmem=_SCRYPT_MAXMEM,
                dklen=_SCRYPT_DKLEN,
            )
        except Exception:
            return False
        return hmac.compare_digest(actual_hash, expected_hash)

    def login(
        self, client: str, password: str, record: AuthRecord
    ) -> Session | None:
        """Authenticate a client, applying a rolling failed-attempt limit."""
        self._validate_client(client)
        with self._lock:
            now = self._clock()
            self._prune_login_failures(now)
            self._check_login_allowed(client)

        verified = self.verify_password(password, record)

        with self._lock:
            now = self._clock()
            self._prune_login_failures(now)
            self._check_login_allowed(client)
            if not verified:
                failures = self._login_failures.setdefault(client, deque())
                failures.append(now)
                return None

            self._login_failures.pop(client, None)
            return self._create_session_locked(now)

    def create_session(self) -> Session:
        """Create and retain a session with independently generated tokens."""
        with self._lock:
            return self._create_session_locked(self._clock())

    def get_session(self, token: str | None) -> Session | None:
        """Return a live session, removing it at its absolute expiry time."""
        if not isinstance(token, str):
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if self._clock() >= session.expires_at:
                self._sessions.pop(token, None)
                return None
            return session

    def validate_csrf(self, session: Session, token: str | None) -> bool:
        """Compare a supplied CSRF token in constant time."""
        if not isinstance(token, str):
            return False
        try:
            expected = session.csrf_token.encode("ascii")
            candidate = token.encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(expected, candidate)

    def revoke(self, token: str | None) -> None:
        """Revoke a session; absent and null tokens are harmless."""
        if not isinstance(token, str):
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _check_login_allowed(self, client: str) -> None:
        failures = self._login_failures.get(client)
        if failures is not None and len(failures) >= _MAX_FAILURES_PER_WINDOW:
            raise LoginRateLimited("login temporarily unavailable")
        if failures is None and len(self._login_failures) >= _MAX_FAILURE_CLIENTS:
            raise LoginRateLimited("login temporarily unavailable")

    def _prune_login_failures(self, now: float) -> None:
        cutoff = now - _FAILURE_WINDOW_SECONDS
        for client, failures in tuple(self._login_failures.items()):
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                self._login_failures.pop(client, None)

    def _create_session_locked(self, now: float) -> Session:
        self._prune_expired_sessions(now)
        if len(self._sessions) >= _MAX_ACTIVE_SESSIONS:
            raise AuthError("authentication operation failed")
        for _ in range(_TOKEN_GENERATION_ATTEMPTS):
            token = self._new_token()
            if token in self._sessions:
                continue
            csrf_token = self._new_token()
            session = Session(
                token=token,
                csrf_token=csrf_token,
                expires_at=now + _SESSION_LIFETIME_SECONDS,
            )
            self._sessions[token] = session
            return session
        raise AuthError("authentication operation failed")

    def _prune_expired_sessions(self, now: float) -> None:
        for token, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(token, None)

    def _new_token(self) -> str:
        return base64.urlsafe_b64encode(self._secure_random(_TOKEN_BYTES)).rstrip(
            b"="
        ).decode("ascii")

    def _secure_random(self, size: int) -> bytes:
        try:
            value = self._random_bytes(size)
        except Exception:
            raise AuthError("authentication operation failed") from None
        if not isinstance(value, bytes) or len(value) != size:
            raise AuthError("authentication operation failed")
        return value

    @staticmethod
    def _encode_new_password(password: str) -> bytes:
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise PasswordPolicyError("password does not meet policy") from None
        try:
            return password.encode("utf-8")
        except UnicodeError:
            raise PasswordPolicyError("password does not meet policy") from None

    @staticmethod
    def _encode_password(password: str) -> bytes:
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValueError
        return password.encode("utf-8")

    @staticmethod
    def _validate_client(client: str) -> None:
        if type(client) is not str or not 1 <= len(client) <= _MAX_CLIENT_LENGTH:
            raise LoginRateLimited("login temporarily unavailable") from None

    @staticmethod
    def _record_has_allowed_parameters(record: AuthRecord) -> bool:
        try:
            return (
                record.algorithm == _ALGORITHM
                and type(record.n) is int
                and record.n == _SCRYPT_N
                and type(record.r) is int
                and record.r == _SCRYPT_R
                and type(record.p) is int
                and record.p == _SCRYPT_P
                and isinstance(record.salt, str)
                and isinstance(record.hash, str)
            )
        except AttributeError:
            return False

    @staticmethod
    def _encode_base64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _preflight_base64(value: str, expected_length: int) -> bytes:
        expected_encoded_length = 4 * ((expected_length + 2) // 3)
        if len(value) != expected_encoded_length:
            raise ValueError
        try:
            return value.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError from None

    @staticmethod
    def _decode_base64(value: bytes, expected_length: int) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError from None
        if len(decoded) != expected_length:
            raise ValueError
        if base64.b64encode(decoded) != value:
            raise ValueError
        return decoded


__all__ = [
    "AuthError",
    "LoginRateLimited",
    "PasswordPolicyError",
    "Session",
    "SettingsAuthService",
]
