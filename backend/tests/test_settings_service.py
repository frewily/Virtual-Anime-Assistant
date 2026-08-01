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
from settings.file_store import SaveJournal, SettingsFileError, SettingsFileStore
from settings.models import SecretMutation
from settings.paths import SettingsPaths
from settings.secrets import SecretStoreUnavailable
from settings.service import (
    SaveResult,
    SessionStatus,
    SettingsService,
    SettingsServiceError,
    VersionedSettingsDraft,
    VoiceSummary,
    create_settings_service,
)
from settings.transactions import (
    SettingsTransactionCoordinator,
    SettingsTransactionError,
)
from settings.validation import (
    SettingsDraft,
    SettingsValidationError,
    SettingsValidationService,
)


class MemorySecretStore:
    def __init__(self, *, available: bool = True) -> None:
        self.values: dict[str, str] = {}
        self.is_available = available
        self.available_calls = 0
        self.get_calls = 0
        self.set_calls = 0
        self.delete_calls = 0

    def available(self) -> bool:
        self.available_calls += 1
        return self.is_available

    def get(self, reference: str) -> str | None:
        self.get_calls += 1
        if not self.is_available:
            raise SecretStoreUnavailable("sensitive backend detail")
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.set_calls += 1
        if not self.is_available:
            raise SecretStoreUnavailable("sensitive backend detail")
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.delete_calls += 1
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
        for interruption in (KeyboardInterrupt, SystemExit, GeneratorExit):
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

    def test_auth_setup_change_invalidates_an_older_draft_revision(self) -> None:
        stale = self.service.get_draft()
        self.service.setup("long-enough-password")

        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(stale)

        self.assertEqual(raised.exception.code, "SETTINGS_CONFLICT")

    def test_login_auth_failure_is_context_free(self) -> None:
        self.service.setup("long-enough-password")

        def fail_login(client, password, record):
            try:
                raise ValueError("private auth detail")
            except ValueError:
                raise AuthError("authentication operation failed") from None

        self.service._auth.login = fail_login
        with self.assertRaises(SettingsServiceError) as raised:
            self.service.login("browser", "long-enough-password")
        self.assertEqual(raised.exception.code, "SETTINGS_AUTH_FAILED")
        self.assertIsNone(raised.exception.__context__)

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

    def test_stale_draft_cannot_overwrite_another_tabs_nonsecret_save(self) -> None:
        first_tab = self.service.get_draft()
        second_tab = self.service.get_draft()
        first_tab.tts.gpt_sovits_url = "http://127.0.0.1:9881"
        self.service.save(first_tab)
        second_tab.tts.audio_max_age_seconds = 123

        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(second_tab)

        self.assertEqual(raised.exception.code, "SETTINGS_CONFLICT")
        stored = self.file_store.load()
        self.assertEqual(stored.tts.gpt_sovits_url, "http://127.0.0.1:9881")
        self.assertEqual(stored.tts.audio_max_age_seconds, 86400)

    def test_stale_secret_delete_has_zero_keychain_activity(self) -> None:
        initial = self.service.get_draft()
        initial.llm.api_key = SecretMutation(operation="replace", value="first-secret")
        self.service.save(initial)
        stale_delete = self.service.get_draft()
        stale_delete.llm.api_key = SecretMutation(operation="delete")

        replacement = self.service.get_draft()
        replacement.llm.api_key = SecretMutation(
            operation="replace", value="second-secret"
        )
        self.service.save(replacement)
        stored_before = self.file_store.load()
        values_before = dict(self.secret_store.values)
        self.secret_store.available_calls = 0
        self.secret_store.get_calls = 0
        self.secret_store.set_calls = 0
        self.secret_store.delete_calls = 0
        voice_loader = Mock(side_effect=AssertionError("catalog must not load"))
        self.service._voice_catalog_loader = voice_loader

        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(stale_delete)

        self.assertEqual(raised.exception.code, "SETTINGS_CONFLICT")
        self.assertEqual(self.file_store.load(), stored_before)
        self.assertEqual(self.secret_store.values, values_before)
        self.assertEqual(
            (
                self.secret_store.available_calls,
                self.secret_store.get_calls,
                self.secret_store.set_calls,
                self.secret_store.delete_calls,
            ),
            (0, 0, 0, 0),
        )
        voice_loader.assert_not_called()

    def test_versioned_draft_json_round_trip_and_invalid_revisions(self) -> None:
        draft = self.service.get_draft()
        self.assertIsInstance(draft, VersionedSettingsDraft)
        restored = VersionedSettingsDraft.model_validate_json(
            draft.model_dump_json()
        )
        self.assertEqual(restored, draft)
        restored.tts.audio_max_age_seconds = 222
        self.service.save(restored)
        self.assertEqual(self.file_store.load().tts.audio_max_age_seconds, 222)
        with self.assertRaises(Exception):
            VersionedSettingsDraft.model_validate(
                {**draft.model_dump(by_alias=True), "revision": "forged"}
            )
        payload = draft.model_dump(by_alias=True)
        payload.pop("revision")
        with self.assertRaises(Exception):
            VersionedSettingsDraft.model_validate(payload)

        forged = draft.model_copy(deep=True)
        forged.revision = "0" * 64
        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(forged)
        self.assertEqual(raised.exception.code, "SETTINGS_CONFLICT")

        bypassed = draft.model_copy(update={"revision": object()})
        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(bypassed)
        self.assertEqual(raised.exception.code, "SETTINGS_CONFLICT")

    def test_unavailable_keychain_allows_nonsecret_save_with_retained_refs(self) -> None:
        initial = self.service.get_draft()
        initial.llm.api_key = SecretMutation(operation="replace", value="first-secret")
        self.service.save(initial)
        reference = self.file_store.load().llm.api_key_ref
        self.secret_store.is_available = False
        draft = self.service.get_draft()
        draft.tts.audio_max_age_seconds = 321

        result = self.service.save(draft)

        self.assertTrue(result.restart_required)
        stored = self.file_store.load()
        self.assertEqual(stored.llm.api_key_ref, reference)
        self.assertEqual(stored.tts.audio_max_age_seconds, 321)

    def test_unavailable_keychain_rejects_secret_mutation_but_allows_retain(self) -> None:
        unavailable = MemorySecretStore(available=False)
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=unavailable,
            voice_catalog_loader=catalog,
            environ={},
        )
        replace = service.get_draft()
        replace.llm.api_key = SecretMutation(
            operation="replace", value="must-not-leak"
        )
        with self.assertRaises(SettingsServiceError) as raised:
            service.save(replace)
        self.assertEqual(raised.exception.code, "KEYCHAIN_UNAVAILABLE")

        persisted = self.file_store.load()
        persisted.llm.api_key_ref = "llm-api-key:existing"
        persisted.llm.enabled = True
        persisted.llm.base_url = "https://api.example.test/v1"
        persisted.llm.model = "model"
        self.file_store.save(persisted)
        retain = service.get_draft()
        retain.tts.audio_max_age_seconds = 444
        result = service.save(retain)
        self.assertTrue(result.restart_required)
        stored = self.file_store.load()
        self.assertEqual(stored.llm.api_key_ref, "llm-api-key:existing")
        self.assertEqual(stored.tts.audio_max_age_seconds, 444)
        self.assertEqual(unavailable.get_calls, 0)

    def test_missing_credential_is_validation_error_only_when_enabled(self) -> None:
        self.secret_store.values.clear()
        persisted = self.file_store.load()
        persisted.llm.api_key_ref = "llm-api-key:missing"
        persisted.llm.enabled = True
        persisted.llm.base_url = "https://api.example.test/v1"
        persisted.llm.model = "model"
        self.file_store.save(persisted)
        enabled = self.service.get_draft()

        with self.assertRaises(SettingsValidationError) as raised:
            self.service.save(enabled)

        self.assertIn("llm.apiKey", raised.exception.fields)
        persisted.llm.enabled = False
        self.file_store.save(persisted)
        disabled = self.service.get_draft()
        disabled.tts.audio_max_age_seconds = 555
        self.service.save(disabled)
        stored = self.file_store.load()
        self.assertEqual(stored.llm.api_key_ref, "llm-api-key:missing")
        self.assertEqual(stored.tts.audio_max_age_seconds, 555)
        presentation = self.service.get_config()
        self.assertTrue(presentation.fields["llm.apiKey"].missing)

    def test_save_uses_entry_snapshot_when_secret_operation_mutates(self) -> None:
        draft = self.service.get_draft()
        draft.llm.api_key = SecretMutation(
            operation="replace", value="entry-secret"
        )

        errors = self._save_while_validation_blocked(
            draft,
            lambda: setattr(
                draft.llm,
                "api_key",
                SecretMutation(operation="retain"),
            ),
        )

        self.assertEqual(errors, [])
        reference = self.file_store.load().llm.api_key_ref
        self.assertIsNotNone(reference)
        self.assertEqual(self.secret_store.values[reference], "entry-secret")

    def test_save_uses_entry_snapshot_when_plain_field_mutates(self) -> None:
        draft = self.service.get_draft()
        draft.tts.audio_max_age_seconds = 111

        errors = self._save_while_validation_blocked(
            draft,
            lambda: setattr(draft.tts, "audio_max_age_seconds", 222),
        )

        self.assertEqual(errors, [])
        self.assertEqual(self.file_store.load().tts.audio_max_age_seconds, 111)

    def _save_while_validation_blocked(
        self,
        draft: VersionedSettingsDraft,
        mutate,
    ) -> list[BaseException]:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_validate = SettingsValidationService.validate

        def blocked_validate(validation_service, received, existing):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("validation release timed out")
            return original_validate(validation_service, received, existing)

        def save() -> None:
            try:
                self.service.save(draft)
            except BaseException as error:
                errors.append(error)

        with patch.object(
            SettingsValidationService,
            "validate",
            new=blocked_validate,
        ):
            worker = threading.Thread(target=save)
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            mutate()
            release.set()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        return errors

    def test_unavailable_keychain_preserves_pending_recovery_journal(self) -> None:
        self.file_store.write_journal(
            SaveJournal(
                transaction_id="pending",
                old_refs=[],
                new_refs=["llm-api-key:orphan"],
                target_refs=["llm-api-key:orphan"],
            )
        )
        self.secret_store.is_available = False
        original = self.file_store.load()
        draft = self.service.get_draft()
        draft.tts.audio_max_age_seconds = 333

        with self.assertRaises(SettingsServiceError) as raised:
            self.service.save(draft)

        self.assertEqual(raised.exception.code, "SETTINGS_SAVE_FAILED")
        self.assertEqual(self.file_store.load(), original)
        self.assertIsNotNone(self.file_store.read_journal())

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
        self.assertEqual(self.secret_store.available_calls, 0)
        self.assertEqual(self.secret_store.get_calls, 0)
        for operation in (service.get_config, service.get_draft):
            with self.assertRaises(SettingsServiceError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "SETTINGS_FILE_INVALID")
            self.assertIsNone(raised.exception.__context__)
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
        self.assertIsNone(raised.exception.__context__)

        invalid_catalog = VoiceCatalog(
            voices=({"id": "voice", "name": 123, "description": "safe"},),
            default_voice="voice",
            fallback_voice="voice",
        )
        invalid_service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            voice_catalog_loader=lambda: invalid_catalog,
            environ={},
        )
        with self.assertRaises(SettingsServiceError) as raised:
            invalid_service.get_voices()
        self.assertEqual(raised.exception.code, "VOICE_CATALOG_INVALID")
        self.assertIsNone(raised.exception.__context__)

    def test_resolver_failure_has_no_retained_exception_context(self) -> None:
        resolver = Mock()
        resolver.resolve.side_effect = ValueError("private resolver secret")
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            resolver=resolver,
            voice_catalog_loader=catalog,
            environ={},
        )

        with self.assertRaises(SettingsServiceError) as raised:
            service.get_config()

        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("private", repr(raised.exception))

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

    def test_failed_secret_transaction_clears_plaintext_from_traceback_frames(self) -> None:
        coordinator = Mock(spec=SettingsTransactionCoordinator)
        coordinator.save.side_effect = SettingsTransactionError(
            "private transaction detail"
        )
        service = SettingsService(
            paths=self.paths,
            file_store=self.file_store,
            secret_store=self.secret_store,
            transaction_coordinator=coordinator,
            voice_catalog_loader=catalog,
            environ={},
        )
        draft = service.get_draft()
        draft.llm.api_key = SecretMutation(
            operation="replace", value="traceback-plaintext-secret"
        )

        with self.assertRaises(SettingsServiceError) as raised:
            service.save(draft)

        frames: list[str] = []
        traceback = raised.exception.__traceback__
        while traceback is not None:
            frames.append(repr(traceback.tb_frame.f_locals))
            traceback = traceback.tb_next
        rendered = " ".join(frames)
        self.assertNotIn("traceback-plaintext-secret", rendered)
        self.assertNotIn("private transaction detail", rendered)
        self.assertIsNone(raised.exception.__context__)


if __name__ == "__main__":
    unittest.main()
