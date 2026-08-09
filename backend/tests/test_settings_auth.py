"""Security-focused tests for local settings authentication."""

from concurrent.futures import ThreadPoolExecutor, wait
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

    def test_base64_length_and_ascii_are_checked_before_decoding(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        valid = service.hash_password("long-enough-password")
        invalid_records = (
            valid.model_copy(update={"salt": "A" * 1_000_000}),
            valid.model_copy(update={"salt": "密" * 24}),
            valid.model_copy(update={"hash": "A" * 1_000_000}),
            valid.model_copy(update={"hash": "密" * 44}),
        )
        decode_calls = 0

        def count_decode_calls(*args: object, **kwargs: object) -> bytes:
            nonlocal decode_calls
            decode_calls += 1
            return b""

        with patch(
            "settings.auth.base64.b64decode", side_effect=count_decode_calls
        ):
            for record in invalid_records:
                self.assertFalse(
                    service.verify_password("long-enough-password", record)
                )

        self.assertEqual(decode_calls, 0)


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

    def test_non_ascii_csrf_candidate_is_rejected_without_an_error(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        session = service.create_session()
        candidate = "密"

        self.assertFalse(service.validate_csrf(session, candidate))

    def test_concurrent_session_creation_and_lookup_is_safe(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())

        with ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lambda _: service.create_session(), range(40)))
            found = list(
                pool.map(lambda item: service.get_session(item.token), sessions)
            )

        self.assertEqual(len({session.token for session in sessions}), 40)
        self.assertEqual(found, sessions)

    def test_expired_sessions_are_globally_pruned_before_creation(self) -> None:
        clock = MutableClock()
        service = SettingsAuthService(
            clock=clock, random_bytes=IncrementingRandom()
        )
        expired = [service.create_session() for _ in range(1000)]

        clock.now = 1800.0
        replacement = service.create_session()

        self.assertEqual(len(service._sessions), 1)
        self.assertIs(service.get_session(replacement.token), replacement)
        self.assertIsNone(service.get_session(expired[0].token))

    def test_active_session_capacity_is_bounded(self) -> None:
        service = SettingsAuthService(random_bytes=IncrementingRandom())
        for _ in range(1024):
            service.create_session()

        with self.assertRaisesRegex(
            AuthError, "^authentication operation failed$"
        ):
            service.create_session()


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

    def test_password_verification_does_not_hold_the_shared_lock(self) -> None:
        lookup_session = self.service.create_session()
        revoke_session = self.service.create_session()
        verification_started = threading.Event()
        release_verification = threading.Event()

        def blocking_verify(password: str, record: AuthRecord) -> bool:
            if password == "blocked-password":
                verification_started.set()
                release_verification.wait(timeout=2.0)
            return False

        with patch.object(
            self.service, "verify_password", side_effect=blocking_verify
        ):
            with ThreadPoolExecutor(max_workers=5) as pool:
                login_future = pool.submit(
                    self.service.login,
                    "blocked-client",
                    "blocked-password",
                    self.record,
                )
                self.assertTrue(verification_started.wait(timeout=1.0))
                create_future = pool.submit(self.service.create_session)
                get_future = pool.submit(
                    self.service.get_session, lookup_session.token
                )
                revoke_future = pool.submit(
                    self.service.revoke, revoke_session.token
                )
                other_login_future = pool.submit(
                    self.service.login,
                    "other-client",
                    "wrong-password",
                    self.record,
                )
                try:
                    _, pending = wait(
                        (
                            create_future,
                            get_future,
                            revoke_future,
                            other_login_future,
                        ),
                        timeout=0.5,
                    )
                    self.assertFalse(pending)
                    self.assertIsNotNone(create_future.result())
                    self.assertIs(get_future.result(), lookup_session)
                    self.assertIsNone(revoke_future.result())
                    self.assertIsNone(other_login_future.result())
                finally:
                    release_verification.set()

                self.assertIsNone(login_future.result(timeout=1.0))

    def test_concurrent_failures_record_only_five_attempts(self) -> None:
        attempts = 8

        def attempt_login(_: int) -> str:
            try:
                result = self.service.login(
                    "shared-client", "wrong-password", self.record
                )
            except LoginRateLimited:
                return "limited"
            self.assertIsNone(result)
            return "failed"

        with patch.object(self.service, "verify_password", return_value=False):
            with ThreadPoolExecutor(max_workers=attempts) as pool:
                results = list(pool.map(attempt_login, range(attempts)))

        self.assertEqual(results.count("failed"), 5)
        self.assertEqual(results.count("limited"), 3)

    def test_concurrent_client_enters_only_five_expensive_verifications(
        self,
    ) -> None:
        attempts = 32
        verification_count = 0
        verification_lock = threading.Lock()
        five_verifying = threading.Event()
        release_verification = threading.Event()

        def expensive_failure(password: str, record: AuthRecord) -> bool:
            nonlocal verification_count
            with verification_lock:
                verification_count += 1
                if verification_count == 5:
                    five_verifying.set()
            release_verification.wait(timeout=2.0)
            return False

        def attempt_login(_: int) -> str:
            try:
                result = self.service.login(
                    "shared-expensive-client", "wrong-password", self.record
                )
            except LoginRateLimited:
                return "limited"
            self.assertIsNone(result)
            return "failed"

        with patch.object(
            self.service, "verify_password", side_effect=expensive_failure
        ):
            with ThreadPoolExecutor(max_workers=attempts) as pool:
                futures = [
                    pool.submit(attempt_login, index) for index in range(attempts)
                ]
                try:
                    self.assertTrue(five_verifying.wait(timeout=1.0))
                    completed, pending = wait(futures, timeout=0.5)
                    with verification_lock:
                        observed_verifications = verification_count
                    self.assertEqual(observed_verifications, 5)
                    self.assertEqual(len(completed), 27)
                    self.assertEqual(len(pending), 5)
                finally:
                    release_verification.set()

                results = [future.result(timeout=1.0) for future in futures]

        self.assertEqual(results.count("failed"), 5)
        self.assertEqual(results.count("limited"), 27)

    def test_verification_exception_releases_client_reservation(self) -> None:
        with patch.object(
            self.service,
            "verify_password",
            side_effect=RuntimeError("sanitized test failure"),
        ):
            for _ in range(5):
                with self.assertRaises(RuntimeError):
                    self.service.login(
                        "exception-client", "wrong-password", self.record
                    )

        with patch.object(self.service, "verify_password", return_value=False):
            self.assertIsNone(
                self.service.login(
                    "exception-client", "wrong-password", self.record
                )
            )
        self.assertEqual(self.service._login_reservations, {})

    def test_expired_failures_for_all_clients_are_globally_pruned(self) -> None:
        with patch.object(self.service, "verify_password", return_value=False):
            for index in range(1000):
                self.assertIsNone(
                    self.service.login(
                        f"old-client-{index}", "wrong-password", self.record
                    )
                )

            self.clock.now = 61.0
            self.assertIsNone(
                self.service.login("new-client", "wrong-password", self.record)
            )

        self.assertEqual(len(self.service._login_failures), 1)
        self.assertIn("new-client", self.service._login_failures)

    def test_active_failed_client_capacity_is_bounded(self) -> None:
        with patch.object(
            self.service, "verify_password", return_value=False
        ) as verify:
            for index in range(1024):
                self.service.login(
                    f"active-client-{index}", "wrong-password", self.record
                )

            with self.assertRaisesRegex(
                LoginRateLimited, "^login temporarily unavailable$"
            ):
                self.service.login(
                    "one-client-too-many", "correct-password", self.record
                )

        self.assertEqual(verify.call_count, 1024)

    def test_invalid_client_keys_are_rejected_without_leaking_input(self) -> None:
        invalid_clients = (["private-client"], "", "private-client" * 30)

        with patch.object(
            self.service, "verify_password", return_value=False
        ) as verify:
            for client in invalid_clients:
                with self.subTest(client_type=type(client)):
                    with self.assertRaises(LoginRateLimited) as raised:
                        self.service.login(
                            client,  # type: ignore[arg-type]
                            "wrong-password",
                            self.record,
                        )
                    rendered = str(raised.exception) + repr(raised.exception)
                    self.assertNotIn("private-client", rendered)

        verify.assert_not_called()


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
