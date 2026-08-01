from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_loader import VoiceCatalog
from settings.auth import AuthError, PasswordPolicyError, Session, SettingsAuthService
from settings.file_store import SettingsFileError, SettingsFileStore
from settings.models import SecretMutation
from settings.paths import SettingsPaths
from settings.secrets import SecretStoreUnavailable
from settings.service import (
    SaveResult,
    SessionStatus,
    SettingsService,
    SettingsServiceError,
    VoiceSummary,
    create_settings_service,
)
from settings.transactions import SettingsTransactionCoordinator
from settings.validation import SettingsDraft, SettingsValidationError


class MemorySecretStore:
    def __init__(self, *, available: bool = True) -> None:
        self.values: dict[str, str] = {}
        self.is_available = available

    def available(self) -> bool:
        return self.is_available

    def get(self, reference: str) -> str | None:
        if not self.is_available:
            raise SecretStoreUnavailable("sensitive backend detail")
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        if not self.is_available:
            raise SecretStoreUnavailable("sensitive backend detail")
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        if not self.is_available:
            raise SecretStoreUnavailable("sensitive backend detail")
        self.values.pop(reference, None)


def catalog() -> VoiceCatalog:
    return VoiceCatalog(
        voices=(
            {
                "id": "character_001",
                "name": "默认音色",
                "description": "温柔",
                "referenceAudio": "/private/voice.wav",
            },
            {
                "id": "character_002",
                "name": "备用音色",
                "description": "活泼",
            },
        ),
        default_voice="character_001",
        fallback_voice="character_001",
    )


class SettingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = SettingsPaths.from_root(Path(self.temporary_directory.name))
        self.file_store = SettingsFileStore(self.paths)
        self.secret_store = MemorySecretStore()
        self.service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=catalog,
            environ={},
        )

    def test_setup_persists_auth_before_creating_session_and_rejects_repeat(self) -> None:
        session = self.service.setup("long-enough-password")

        persisted = self.file_store.load()
        self.assertIsNotNone(persisted.auth)
        self.assertIsInstance(session, Session)
        self.assertIsNotNone(self.service.session_status(session.token).csrf_token)
        with self.assertRaises(SettingsServiceError) as raised:
            self.service.setup("another-long-password")
        self.assertEqual(raised.exception.code, "SETTINGS_ALREADY_INITIALIZED")
        self.assertNotIn("password", repr(raised.exception).lower())

    def test_setup_reuses_auth_password_policy(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            self.service.setup("short")
        self.assertIsNone(self.file_store.load().auth)

    def test_setup_write_failure_creates_no_session_or_memory_auth_state(self) -> None:
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=catalog,
            environ={},
        )
        original_save = self.file_store.save
        self.file_store.save = Mock(side_effect=SettingsFileError("private path"))

        with self.assertRaises(SettingsServiceError) as raised:
            service.setup("long-enough-password")

        self.assertEqual(raised.exception.code, "SETTINGS_SAVE_FAILED")
        self.assertFalse(service.session_status(None).initialized)
        self.assertFalse(service.session_status("unknown").authenticated)
        self.file_store.save = original_save
        self.assertIsNone(self.file_store.load().auth)

    def test_setup_session_failure_rolls_back_persisted_auth(self) -> None:
        auth = SettingsAuthService()
        for _ in range(1024):
            auth.create_session()
        sessions_before = len(auth._sessions)
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            auth_service=auth,
            voice_catalog_loader=catalog,
            environ={},
        )
        original = self.file_store.load()

        with self.assertRaises(SettingsServiceError) as raised:
            service.setup("long-enough-password")

        self.assertEqual(raised.exception.code, "SETTINGS_AUTH_FAILED")
        self.assertEqual(self.file_store.load(), original)
        self.assertEqual(len(auth._sessions), sessions_before)
        self.assertIsNone(raised.exception.__cause__)

    def test_setup_rollback_never_overwrites_a_third_party_update(self) -> None:
        real_auth = SettingsAuthService()
        third_party_transaction = SettingsTransactionCoordinator(
            self.file_store, self.secret_store
        )

        class ConcurrentUpdateThenFailAuth:
            def hash_password(inner_self, password: str):
                return real_auth.hash_password(password)

            def create_session(inner_self):
                committed = self.file_store.load()
                changed_llm = committed.llm.model_copy(
                    update={"model": "third-party-model"}
                )
                third_party = committed.model_copy(update={"llm": changed_llm})
                third_party_transaction.save(committed, third_party, {})
                raise AuthError("private session failure")

            def get_session(inner_self, token):
                return None

            def revoke(inner_self, token):
                return None

            def login(inner_self, client, password, record):
                return None

        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            auth_service=ConcurrentUpdateThenFailAuth(),
            voice_catalog_loader=catalog,
            environ={},
        )

        with self.assertRaises(SettingsServiceError) as raised:
            service.setup("long-enough-password")

        self.assertEqual(raised.exception.code, "SETTINGS_SETUP_STATE_UNCERTAIN")
        stored = self.file_store.load()
        self.assertIsNotNone(stored.auth)
        self.assertEqual(stored.llm.model, "third-party-model")
        self.assertNotIn("private", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_setup_interrupts_roll_back_auth_and_propagate_unchanged(self) -> None:
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    paths = SettingsPaths.from_root(Path(directory))
                    file_store = SettingsFileStore(paths)
                    secret_store = MemorySecretStore()
                    calls = 0

                    def interrupting_random(size: int) -> bytes:
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            return b"s" * size
                        raise interruption("private interruption")

                    auth = SettingsAuthService(random_bytes=interrupting_random)
                    service = SettingsService(
                        paths=paths,
                        file_store=file_store,
                        secret_store=secret_store,
                        auth_service=auth,
                        voice_catalog_loader=catalog,
                        environ={},
                    )
                    original = file_store.load()

                    with self.assertRaises(interruption) as raised:
                        service.setup("long-enough-password")

                    self.assertEqual(str(raised.exception), "private interruption")
                    self.assertEqual(file_store.load(), original)
                    self.assertEqual(len(auth._sessions), 0)

    def test_setup_interrupt_still_wins_when_rollback_detects_concurrent_update(self) -> None:
        real_auth = SettingsAuthService()
        third_party_transaction = SettingsTransactionCoordinator(
            self.file_store, self.secret_store
        )

        class CountingTransaction:
            def __init__(inner_self) -> None:
                inner_self.delegate = SettingsTransactionCoordinator(
                    self.file_store, self.secret_store
                )
                inner_self.save_calls = 0

            def save(inner_self, current, proposed, replacements):
                inner_self.save_calls += 1
                return inner_self.delegate.save(current, proposed, replacements)

            def recover(inner_self):
                return inner_self.delegate.recover()

        transaction = CountingTransaction()

        class ConcurrentUpdateThenInterruptAuth:
            def hash_password(inner_self, password: str):
                return real_auth.hash_password(password)

            def create_session(inner_self):
                committed = self.file_store.load()
                changed_llm = committed.llm.model_copy(
                    update={"model": "third-party-model"}
                )
                third_party = committed.model_copy(update={"llm": changed_llm})
                third_party_transaction.save(committed, third_party, {})
                raise KeyboardInterrupt("private interruption")

            def get_session(inner_self, token):
                return None

            def revoke(inner_self, token):
                return None

            def login(inner_self, client, password, record):
                return None

        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            transaction_coordinator=transaction,
            auth_service=ConcurrentUpdateThenInterruptAuth(),
            voice_catalog_loader=catalog,
            environ={},
        )

        with self.assertRaises(KeyboardInterrupt) as raised:
            service.setup("long-enough-password")

        self.assertEqual(str(raised.exception), "private interruption")
        self.assertEqual(transaction.save_calls, 2)
        stored = self.file_store.load()
        self.assertIsNotNone(stored.auth)
        self.assertEqual(stored.llm.model, "third-party-model")

    def test_concurrent_setup_has_single_winner(self) -> None:
        other = SettingsService(
            paths=self.paths,
            file_store=SettingsFileStore(self.paths),
            secret_store=self.secret_store,
            voice_catalog_loader=catalog,
            environ={},
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def setup(service: SettingsService, password: str) -> None:
            barrier.wait()
            try:
                service.setup(password)
                outcomes.append("session")
            except SettingsServiceError as error:
                outcomes.append(error.code)

        threads = [
            threading.Thread(
                target=setup, args=(self.service, "first-long-password")
            ),
            threading.Thread(
                target=setup, args=(other, "second-long-password")
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes.count("session"), 1)
        self.assertEqual(self.file_store.load().auth is not None, True)

    def test_session_status_login_and_logout(self) -> None:
        setup_session = self.service.setup("long-enough-password")
        status = self.service.session_status(setup_session.token)
        self.assertTrue(status.initialized)
        self.assertTrue(status.authenticated)
        self.assertEqual(status.csrf_token, setup_session.csrf_token)
        self.assertNotIn(setup_session.token, repr(status))
        self.assertNotIn(setup_session.csrf_token, repr(status))

        self.service.logout(setup_session.token)
        self.assertFalse(self.service.session_status(setup_session.token).authenticated)
        self.assertIsNone(self.service.login("browser", "wrong-password"))
        login_session = self.service.login("browser", "long-enough-password")
        self.assertIsNotNone(login_session)

    def test_uninitialized_login_returns_none(self) -> None:
        self.assertIsNone(self.service.login("browser", "long-enough-password"))
        self.assertEqual(
            self.service.session_status(None),
            SessionStatus(initialized=False, authenticated=False),
        )

    def test_config_and_draft_are_secret_free(self) -> None:
        draft = self.service.get_draft()
        draft.llm.enabled = True
        draft.llm.base_url = "https://api.example.test/v1"
        draft.llm.model = "model-a"
        draft.llm.api_key = SecretMutation(
            operation="replace", value="top-secret-api-key"
        )
        draft.qq.access_token = SecretMutation(
            operation="replace", value="0123456789abcdef"
        )
        self.service.save(draft)

        presentation = self.service.get_config()
        safe_draft = self.service.get_draft()
        combined = " ".join(
            (
                repr(presentation),
                presentation.model_dump_json(),
                repr(safe_draft),
                safe_draft.model_dump_json(),
            )
        )
        self.assertNotIn("top-secret-api-key", combined)
        self.assertNotIn("0123456789abcdef", combined)
        self.assertTrue(presentation.fields["llm.apiKey"].configured)
        self.assertEqual(safe_draft.llm.api_key.operation.value, "retain")
        self.assertIsNone(safe_draft.llm.api_key.value)

    def test_save_retain_replace_and_delete_secret(self) -> None:
        draft = self.service.get_draft()
        draft.llm.api_key = SecretMutation(operation="replace", value="first-secret")
        result = self.service.save(draft)
        first_ref = self.file_store.load().llm.api_key_ref
        self.assertEqual(result, SaveResult(restart_required=True))
        self.assertEqual(self.secret_store.values[first_ref], "first-secret")

        retained = self.service.get_draft()
        self.service.save(retained)
        self.assertEqual(self.file_store.load().llm.api_key_ref, first_ref)

        replaced = self.service.get_draft()
        replaced.llm.api_key = SecretMutation(
            operation="replace", value="second-secret"
        )
        self.service.save(replaced)
        second_ref = self.file_store.load().llm.api_key_ref
        self.assertNotEqual(second_ref, first_ref)
        self.assertNotIn(first_ref, self.secret_store.values)
        self.assertEqual(self.secret_store.values[second_ref], "second-secret")

        deleted = self.service.get_draft()
        deleted.llm.api_key = SecretMutation(operation="delete")
        self.service.save(deleted)
        self.assertIsNone(self.file_store.load().llm.api_key_ref)
        self.assertNotIn(second_ref, self.secret_store.values)

    def test_validation_and_voice_snapshot_are_shared_by_save(self) -> None:
        calls = 0

        def changing_catalog() -> VoiceCatalog:
            nonlocal calls
            calls += 1
            return catalog()

        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=changing_catalog,
            environ={},
        )
        draft = service.get_draft()
        draft.tts.default_voice_id = "character_002"
        service.save(draft)
        self.assertEqual(calls, 1)

    def test_invalid_draft_does_not_change_persisted_snapshot(self) -> None:
        before = self.file_store.load()
        draft = self.service.get_draft()
        draft.tts.default_voice_id = "unknown"
        with self.assertRaises(SettingsValidationError):
            self.service.save(draft)
        self.assertEqual(self.file_store.load(), before)

    def test_keychain_unavailable_fails_closed_and_redacted(self) -> None:
        unavailable = MemorySecretStore(available=False)
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=unavailable,
            voice_catalog_loader=catalog,
            environ={},
        )
        draft = service.get_draft()
        draft.llm.api_key = SecretMutation(
            operation="replace", value="must-not-leak"
        )
        with self.assertRaises(SettingsServiceError) as raised:
            service.save(draft)
        error = raised.exception
        self.assertEqual(error.code, "KEYCHAIN_UNAVAILABLE")
        self.assertNotIn("must-not-leak", repr(error))
        self.assertNotIn("sensitive", str(error.__context__))

    def test_environment_owned_value_and_secret_reject_mutation(self) -> None:
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=catalog,
            environ={
                "ASSISTANT_LLM_MODEL": "environment-model",
                "ASSISTANT_LLM_API_KEY": "environment-secret",
            },
        )
        draft = service.get_draft()
        draft.llm.model = "browser-model"
        draft.llm.api_key = SecretMutation(operation="delete")

        with self.assertRaises(SettingsValidationError) as raised:
            service.save(draft)

        self.assertEqual(
            set(raised.exception.fields), {"llm.model", "llm.apiKey"}
        )
        self.assertEqual(self.file_store.load().llm.model, None)
        self.assertNotIn("environment-secret", repr(raised.exception))

    def test_corrupt_file_runtime_falls_back_but_config_api_fails_closed(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        original = b'{"schemaVersion":1,"auth":{"hash":"private-corruption"}}'
        self.paths.settings_file.write_bytes(original)
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=catalog,
            environ={"ASSISTANT_LLM_MODEL": "environment-model"},
        )

        runtime = service.runtime_settings()
        self.assertEqual(runtime.llm.model, "environment-model")
        for operation in (service.get_config, service.get_draft):
            with self.assertRaises(SettingsServiceError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "SETTINGS_FILE_INVALID")
        with self.assertRaises(SettingsServiceError) as raised:
            service.save(SettingsDraft())
        self.assertEqual(raised.exception.code, "SETTINGS_FILE_INVALID")
        self.assertEqual(self.paths.settings_file.read_bytes(), original)

    def test_recover_delegates_without_hiding_invalid_settings(self) -> None:
        coordinator = Mock(spec=SettingsTransactionCoordinator)
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            transaction_coordinator=coordinator,
            voice_catalog_loader=catalog,
            environ={},
        )
        service.recover()
        coordinator.recover.assert_called_once_with()

        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.settings_file.write_text("private-corruption", encoding="utf-8")
        coordinator.reset_mock()
        with self.assertRaises(SettingsServiceError) as raised:
            service.recover()
        self.assertEqual(raised.exception.code, "SETTINGS_FILE_INVALID")
        coordinator.recover.assert_not_called()

    def test_voice_summaries_are_safe_frozen_and_defensively_copied(self) -> None:
        first = self.service.get_voices()
        second = self.service.get_voices()
        self.assertIsNot(first, second)
        self.assertEqual(first[0], VoiceSummary(
            id="character_001", name="默认音色", description="温柔"
        ))
        self.assertNotIn("referenceAudio", first[0].model_dump_json())
        with self.assertRaises(Exception):
            first[0].name = "篡改"
        first.clear()
        self.assertEqual(len(second), 2)

    def test_voice_catalog_failure_is_stable_and_redacted(self) -> None:
        def fail() -> VoiceCatalog:
            raise ValueError("/private/path/secret-voice.yaml")

        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=fail,
            environ={},
        )
        with self.assertRaises(SettingsServiceError) as raised:
            service.get_voices()
        self.assertEqual(raised.exception.code, "VOICE_CATALOG_INVALID")
        self.assertNotIn("/private/path", repr(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_result_models_are_strict_frozen_and_safe(self) -> None:
        status = SessionStatus(
            initialized=True,
            authenticated=True,
            csrf_token="csrf-private",
            expires_at=42.0,
        )
        self.assertNotIn("csrf-private", repr(status))
        self.assertEqual(
            status.model_dump(by_alias=False)["csrf_token"], "csrf-private"
        )
        with self.assertRaises(Exception):
            status.authenticated = False
        with self.assertRaises(TypeError):
            status.model_copy(update={"authenticated": False})
        with self.assertRaises(Exception):
            SaveResult(restartRequired="true")
        with self.assertRaises(TypeError):
            SaveResult(restart_required=True).model_copy(
                update={"restart_required": "true"}
            )
        with self.assertRaises(Exception):
            VoiceSummary(id="x", name="n", description="d", extra="bad")

    def test_factory_and_constructor_do_no_io(self) -> None:
        with (
            patch.object(SettingsFileStore, "load", side_effect=AssertionError("file")),
            patch(
                "settings.secrets.KeychainSecretStore.available",
                side_effect=AssertionError("keychain"),
            ),
            patch(
                "settings.service.load_voice_catalog",
                side_effect=AssertionError("voice"),
            ),
            patch.object(Path, "mkdir", side_effect=AssertionError("directory")),
        ):
            service = create_settings_service(self.paths)
        self.assertIsInstance(service, SettingsService)
        self.assertFalse(self.paths.settings_file.exists())

    def test_service_errors_do_not_leak_context_traceback_or_json(self) -> None:
        error = SettingsServiceError("SETTINGS_SAVE_FAILED")
        payload = " ".join((str(error), repr(error), error.json(), json.dumps(error.to_dict())))
        self.assertNotIn("secret", payload.lower())
        self.assertEqual(error.code, "SETTINGS_SAVE_FAILED")


if __name__ == "__main__":
    unittest.main()
