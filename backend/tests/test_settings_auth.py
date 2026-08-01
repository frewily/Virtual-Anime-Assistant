"""Security-focused tests for local settings authentication."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hmac
import threading
import traceback
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.auth import (
    AuthError,
    LoginRateLimited,
    PasswordPolicyError,
    SettingsAuthService,
)
from settings.models import AuthRecord


class MutableClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class IncrementingRandom:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self, size: int) -> bytes:
        with self._lock:
            value = self._value
            self._value += 1
        return value.to_bytes(size, "big")


class SequenceRandom:
    def __init__(self, values: list[bytes]) -> None:
        self._values = iter(values)

    def __call__(self, size: int) -> bytes:
        value = next(self._values)
        if len(value) != size:
            raise AssertionError(f"expected {size} bytes, got {len(value)}")
        return value


class PasswordHashTests(unittest.TestCase):
    def test_hash_verifies_password_and_uses_different_salts(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())

        first = service.hash_password("long-enough-password")
        second = service.hash_password("long-enough-password")

        self.assertTrue(service.verify_password("long-enough-password", first))
        self.assertFalse(service.verify_password("incorrect-password", first))
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.hash, second.hash)
        self.assertEqual(first.algorithm, "scrypt")
        self.assertEqual((first.n, first.r, first.p), (32768, 8, 1))

    def test_password_character_boundaries_and_unicode(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())

        ten_unicode_characters = "密" * 10
        unicode_record = service.hash_password(ten_unicode_characters)
        self.assertTrue(service.verify_password(ten_unicode_characters, unicode_record))
        self.assertTrue(
            service.verify_password("x" * 128, service.hash_password("x" * 128))
        )

        for invalid in ("x" * 9, "x" * 129):
            with self.subTest(length=len(invalid)):
                with self.assertRaisesRegex(
                    PasswordPolicyError, "^password does not meet policy$"
                ):
                    service.hash_password(invalid)

    def test_tampered_records_do_not_run_untrusted_scrypt_costs(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        valid = service.hash_password("long-enough-password")
        tampered_records = (
            valid.model_copy(update={"algorithm": "unknown"}),
            valid.model_copy(update={"n": 2**40}),
            valid.model_copy(update={"r": 16}),
            valid.model_copy(update={"p": 2}),
            valid.model_copy(update={"salt": "not base64!"}),
            valid.model_copy(update={"salt": "YQ=="}),
            valid.model_copy(update={"hash": "not base64!"}),
            valid.model_copy(update={"hash": "YQ=="}),
        )

        with patch("settings.auth.hashlib.scrypt") as mocked_scrypt:
            for record in tampered_records:
                with self.subTest(record=record):
                    self.assertFalse(
                        service.verify_password("long-enough-password", record)
                    )
        mocked_scrypt.assert_not_called()

    def test_verify_returns_false_for_scrypt_errors(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        record = service.hash_password("long-enough-password")

        with patch("settings.auth.hashlib.scrypt", side_effect=ValueError("failure")):
            self.assertFalse(service.verify_password("long-enough-password", record))


class SessionTests(unittest.TestCase):
    def test_session_expires_at_exactly_1800_seconds(self) -> None:
        clock = MutableClock(100.0)
        service = SettingsAuthService(clock=clock, random_bytes=IncrementingRandom())

        session = service.create_session()

        self.assertEqual(session.expires_at, 1900.0)
        clock.now = 1899.999
        self.assertIs(service.get_session(session.token), session)
        clock.now = 1900.0
        self.assertIsNone(service.get_session(session.token))
        clock.now = 1800.0
        self.assertIsNone(service.get_session(session.token))

    def test_revoke_and_none_operations_are_idempotent(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        session = service.create_session()

        service.revoke(None)
        self.assertIsNone(service.get_session(None))
        service.revoke(session.token)
        service.revoke(session.token)

        self.assertIsNone(service.get_session(session.token))

    def test_token_collision_is_retried_and_repr_hides_both_tokens(self) -> None:
        token_a = b"a" * 32
        csrf_a = b"b" * 32
        colliding_token = b"a" * 32
        discarded_csrf = b"c" * 32
        token_b = b"d" * 32
        csrf_b = b"e" * 32
        service = SettingsAuthService(
            random_bytes=SequenceRandom(
                [token_a, csrf_a, colliding_token, discarded_csrf, token_b, csrf_b]
            )
        )

        first = service.create_session()
        second = service.create_session()

        self.assertNotEqual(first.token, second.token)
        for session in (first, second):
            rendered = repr(session)
            self.assertNotIn(session.token, rendered)
            self.assertNotIn(session.csrf_token, rendered)

    def test_repeated_token_collision_raises_sanitized_auth_error(self) -> None:
        repeated = b"z" * 32
        service = SettingsAuthService(random_bytes=lambda size: repeated)
        first = service.create_session()

        with self.assertRaises(AuthError) as raised:
            service.create_session()

        rendered = "".join(
            traceback.format_exception(
                type(raised.exception), raised.exception, raised.exception.__traceback__
            )
        )
        self.assertNotIn(first.token, rendered)
        self.assertNotIn(first.csrf_token, rendered)

    def test_csrf_validation_uses_constant_time_comparison(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        session = service.create_session()

        with patch(
            "settings.auth.hmac.compare_digest", wraps=hmac.compare_digest
        ) as compare:
            self.assertTrue(service.validate_csrf(session, session.csrf_token))
            self.assertFalse(service.validate_csrf(session, "wrong-token"))
            self.assertFalse(service.validate_csrf(session, None))

        self.assertEqual(compare.call_count, 2)

    def test_concurrent_session_creation_and_lookup_is_safe(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())

        with ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lambda _: service.create_session(), range(40)))
            found = list(
                pool.map(lambda item: service.get_session(item.token), sessions)
            )

        self.assertEqual(len({session.token for session in sessions}), 40)
        self.assertEqual(found, sessions)


class LoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.service = SettingsAuthService(
            clock=self.clock, random_bytes=IncrementingRandom()
        )
        self.record = self.service.hash_password("correct-password")

    def test_five_failures_return_none_and_sixth_attempt_is_rate_limited(self) -> None:
        for _ in range(5):
            self.assertIsNone(
                self.service.login("local-browser", "wrong-password", self.record)
            )

        with self.assertRaises(LoginRateLimited):
            self.service.login("local-browser", "correct-password", self.record)

    def test_rate_limit_recovers_after_rolling_window(self) -> None:
        for _ in range(5):
            self.assertIsNone(
                self.service.login("local-browser", "wrong-password", self.record)
            )

        self.clock.now = 60.0
        session = self.service.login(
            "local-browser", "correct-password", self.record
        )

        self.assertIsNotNone(session)

    def test_success_clears_failures(self) -> None:
        for _ in range(4):
            self.assertIsNone(
                self.service.login("local-browser", "wrong-password", self.record)
            )
        self.assertIsNotNone(
            self.service.login("local-browser", "correct-password", self.record)
        )

        for _ in range(5):
            self.assertIsNone(
                self.service.login("local-browser", "wrong-password", self.record)
            )

    def test_rate_limits_are_isolated_by_client(self) -> None:
        for _ in range(5):
            self.service.login("client-a", "wrong-password", self.record)

        self.assertIsNotNone(
            self.service.login("client-b", "correct-password", self.record)
        )
        with self.assertRaises(LoginRateLimited):
            self.service.login("client-a", "correct-password", self.record)


class ErrorRedactionTests(unittest.TestCase):
    def test_password_policy_error_does_not_contain_input(self) -> None:
        password = "short-secret"
        service = SettingsAuthService(random_bytes=IncrementingRandom())

        with self.assertRaises(PasswordPolicyError) as raised:
            service.hash_password(password[:5])

        rendered = "".join(
            traceback.format_exception(
                type(raised.exception), raised.exception, raised.exception.__traceback__
            )
        )
        self.assertNotIn(password[:5], str(raised.exception))
        self.assertNotIn(password[:5], repr(raised.exception))
        self.assertNotIn(password[:5], rendered)

    def test_auth_record_repr_never_contains_plaintext_password(self) -> None:
        password = "long-enough-password"
        record = SettingsAuthService(
            random_bytes=IncrementingRandom()
        ).hash_password(password)

        self.assertIsInstance(record, AuthRecord)
        self.assertNotIn(password, repr(record))


if __name__ == "__main__":
    unittest.main()
