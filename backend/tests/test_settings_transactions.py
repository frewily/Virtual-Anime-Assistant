"""Tests for recoverable settings and credential-store transactions."""

import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.file_store import SettingsFileStore
from settings.models import LLMSettings, PersistedSettings, QQSettings
from settings.paths import SettingsPaths
from settings.transactions import (
    SettingsTransactionCoordinator,
    SettingsTransactionError,
)


class MemorySecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_set_at: int | None = None
        self.fail_delete_at: int | None = None
        self._set_count = 0
        self._delete_count = 0

    def available(self) -> bool:
        return True

    def get(self, reference: str) -> str | None:
        self.calls.append(("get", reference, None))
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self._set_count += 1
        if self.fail_set_at == self._set_count:
            raise RuntimeError("private-sentinel")
        self.calls.append(("set", reference, value))
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self._delete_count += 1
        if self.fail_delete_at == self._delete_count:
            raise RuntimeError("private-sentinel")
        self.calls.append(("delete", reference, None))
        self.values.pop(reference, None)


class FaultingFileStore:
    def __init__(self, delegate: SettingsFileStore) -> None:
        self.delegate = delegate
        self.fail_save = False
        self.fail_delete_journal = False

    @property
    def paths(self) -> SettingsPaths:
        return self.delegate.paths

    def load(self) -> PersistedSettings:
        return self.delegate.load()

    def save(self, settings: PersistedSettings) -> None:
        if self.fail_save:
            raise RuntimeError("private-sentinel")
        self.delegate.save(settings)

    def read_journal(self):
        return self.delegate.read_journal()

    def write_journal(self, journal) -> None:
        self.delegate.write_journal(journal)

    def delete_journal(self) -> None:
        if self.fail_delete_journal:
            raise RuntimeError("private-sentinel")
        self.delegate.delete_journal()


class ReferenceFactory:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        count = self.counts.get(prefix, 0) + 1
        self.counts[prefix] = count
        return f"{prefix}:version-{count}"


class SettingsTransactionTests(unittest.TestCase):
    secret = "test-secret-private-sentinel"

    def make_store(self, root: Path) -> SettingsFileStore:
        return SettingsFileStore(SettingsPaths.from_root(root))

    def make_current(self) -> PersistedSettings:
        return PersistedSettings(
            llm=LLMSettings(api_key_ref="llm-api-key:old"),
            qq=QQSettings(access_token_ref="qq-access-token:old"),
        )

    def assert_error_redacted(self, error: BaseException) -> None:
        for rendered in (
            str(error),
            repr(error),
            "".join(traceback.format_exception(error)),
        ):
            self.assertNotIn("private-sentinel", rendered)
            self.assertNotIn(self.secret, rendered)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def assert_loaded_refs_have_secrets(
        self, file_store: SettingsFileStore, secret_store: MemorySecretStore
    ) -> None:
        loaded = file_store.load()
        for reference in (loaded.llm.api_key_ref, loaded.qq.access_token_ref):
            if reference is not None:
                self.assertIn(reference, secret_store.values)

    def test_replace_commits_new_reference_and_removes_old_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            file_store = self.make_store(Path(temporary_directory))
            current = self.make_current()
            file_store.save(current)
            secret_store = MemorySecretStore(
                {
                    "llm-api-key:old": "old-llm",
                    "qq-access-token:old": "old-qq",
                }
            )
            proposed = current.model_copy(deep=True)
            coordinator = SettingsTransactionCoordinator(
                file_store, secret_store, ReferenceFactory()
            )

            saved = coordinator.save(
                current, proposed, {"llm.apiKey": self.secret}
            )

            self.assertEqual(saved.llm.api_key_ref, "llm-api-key:version-1")
            self.assertEqual(file_store.load(), saved)
            self.assertEqual(
                secret_store.values,
                {
                    "llm-api-key:version-1": self.secret,
                    "qq-access-token:old": "old-qq",
                },
            )
            self.assertIsNone(file_store.read_journal())
            serialized_settings = file_store.paths.settings_file.read_text("utf-8")
            self.assertNotIn(self.secret, serialized_settings)
            self.assertNotIn("old-llm", serialized_settings)

    def test_retain_has_no_secret_side_effects_and_delete_removes_old(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            file_store = self.make_store(Path(temporary_directory))
            current = self.make_current()
            file_store.save(current)
            secret_store = MemorySecretStore(
                {
                    "llm-api-key:old": "old-llm",
                    "qq-access-token:old": "old-qq",
                }
            )
            proposed = current.model_copy(
                deep=True,
                update={
                    "qq": current.qq.model_copy(
                        update={"access_token_ref": None}
                    )
                },
            )

            saved = SettingsTransactionCoordinator(
                file_store, secret_store, ReferenceFactory()
            ).save(current, proposed, {})

            self.assertEqual(saved.llm.api_key_ref, "llm-api-key:old")
            self.assertIsNone(saved.qq.access_token_ref)
            self.assertEqual(secret_store.values, {"llm-api-key:old": "old-llm"})
            self.assertEqual(
                secret_store.calls,
                [("delete", "qq-access-token:old", None)],
            )

    def test_invalid_replacements_fail_before_any_side_effect(self) -> None:
        invalid_replacements = (
            {"unknown.secret": "value"},
            {"llm.apiKey": ""},
            {"qq.accessToken": "   "},
        )

        for replacements in invalid_replacements:
            with self.subTest(replacements=replacements), tempfile.TemporaryDirectory() as temporary_directory:
                file_store = self.make_store(Path(temporary_directory))
                current = self.make_current()
                file_store.save(current)
                original_settings = file_store.paths.settings_file.read_bytes()
                secret_store = MemorySecretStore()

                with self.assertRaises(SettingsTransactionError) as raised:
                    SettingsTransactionCoordinator(
                        file_store, secret_store, ReferenceFactory()
                    ).save(current, current, replacements)

                self.assert_error_redacted(raised.exception)
                self.assertEqual(secret_store.calls, [])
                self.assertEqual(
                    file_store.paths.settings_file.read_bytes(), original_settings
                )
                self.assertIsNone(file_store.read_journal())

    def test_crashes_at_each_commit_boundary_are_recoverable(self) -> None:
        scenarios = (
            ("after_journal", 1, None, False),
            ("after_partial_new_secrets", 2, None, False),
            ("after_settings_save", None, 1, False),
            ("after_old_delete", None, None, True),
        )

        for name, fail_set_at, fail_delete_at, fail_journal_delete in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                real_file_store = self.make_store(Path(temporary_directory))
                file_store = FaultingFileStore(real_file_store)
                current = self.make_current()
                real_file_store.save(current)
                secret_store = MemorySecretStore(
                    {
                        "llm-api-key:old": "old-llm",
                        "qq-access-token:old": "old-qq",
                        "unlisted:keep": "unlisted",
                    }
                )
                secret_store.fail_set_at = fail_set_at
                secret_store.fail_delete_at = fail_delete_at
                file_store.fail_delete_journal = fail_journal_delete
                coordinator = SettingsTransactionCoordinator(
                    file_store, secret_store, ReferenceFactory()
                )

                with self.assertRaises(SettingsTransactionError) as raised:
                    coordinator.save(
                        current,
                        current,
                        {
                            "llm.apiKey": f"{self.secret}-llm",
                            "qq.accessToken": f"{self.secret}-qq",
                        },
                    )

                self.assert_error_redacted(raised.exception)
                self.assertIsNotNone(real_file_store.read_journal())
                secret_store.fail_set_at = None
                secret_store.fail_delete_at = None
                file_store.fail_delete_journal = False
                SettingsTransactionCoordinator(
                    real_file_store, secret_store, ReferenceFactory()
                ).recover()

                self.assert_loaded_refs_have_secrets(real_file_store, secret_store)
                self.assertEqual(secret_store.values["unlisted:keep"], "unlisted")
                self.assertIsNone(real_file_store.read_journal())

    def test_delete_only_recovery_distinguishes_before_and_after_settings_save(self) -> None:
        for name, fail_save, fail_delete in (
            ("before_settings_save", True, False),
            ("after_settings_save", False, True),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                real_file_store = self.make_store(Path(temporary_directory))
                file_store = FaultingFileStore(real_file_store)
                current = self.make_current()
                real_file_store.save(current)
                secret_store = MemorySecretStore(
                    {
                        "llm-api-key:old": "old-llm",
                        "qq-access-token:old": "old-qq",
                    }
                )
                proposed = PersistedSettings(
                    llm=LLMSettings(api_key_ref="llm-api-key:old"),
                    qq=QQSettings(access_token_ref=None),
                )
                file_store.fail_save = fail_save
                secret_store.fail_delete_at = 1 if fail_delete else None

                with self.assertRaises(SettingsTransactionError):
                    SettingsTransactionCoordinator(
                        file_store, secret_store, ReferenceFactory()
                    ).save(current, proposed, {})

                journal = real_file_store.read_journal()
                self.assertIsNotNone(journal)
                self.assertEqual(journal.target_refs, ["llm-api-key:old"])
                file_store.fail_save = False
                secret_store.fail_delete_at = None
                SettingsTransactionCoordinator(
                    real_file_store, secret_store, ReferenceFactory()
                ).recover()

                if fail_save:
                    self.assertEqual(real_file_store.load(), current)
                    self.assertIn("qq-access-token:old", secret_store.values)
                else:
                    self.assertEqual(real_file_store.load(), proposed)
                    self.assertNotIn("qq-access-token:old", secret_store.values)

    def test_recovery_cleanup_failure_keeps_journal_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            real_file_store = self.make_store(Path(temporary_directory))
            file_store = FaultingFileStore(real_file_store)
            current = self.make_current()
            real_file_store.save(current)
            secret_store = MemorySecretStore(
                {
                    "llm-api-key:old": "old-llm",
                    "qq-access-token:old": "old-qq",
                    "unlisted:keep": "unlisted",
                }
            )
            secret_store.fail_delete_at = 1

            with self.assertRaises(SettingsTransactionError):
                SettingsTransactionCoordinator(
                    file_store, secret_store, ReferenceFactory()
                ).save(
                    current,
                    current,
                    {"llm.apiKey": self.secret},
                )

            secret_store.fail_delete_at = 2
            with self.assertRaises(SettingsTransactionError) as raised:
                SettingsTransactionCoordinator(
                    real_file_store, secret_store, ReferenceFactory()
                ).recover()

            self.assert_error_redacted(raised.exception)
            self.assertIsNotNone(real_file_store.read_journal())
            self.assertEqual(secret_store.values["unlisted:keep"], "unlisted")
            secret_store.fail_delete_at = None
            SettingsTransactionCoordinator(
                real_file_store, secret_store, ReferenceFactory()
            ).recover()
            self.assertIsNone(real_file_store.read_journal())
            self.assertEqual(secret_store.values["unlisted:keep"], "unlisted")

    def test_serialized_files_and_errors_never_contain_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            file_store = self.make_store(Path(temporary_directory))
            current = self.make_current()
            file_store.save(current)
            secret_store = MemorySecretStore(
                {
                    "llm-api-key:old": "old-llm",
                    "qq-access-token:old": "old-qq",
                }
            )
            secret_store.fail_set_at = 1

            with self.assertRaises(SettingsTransactionError) as raised:
                SettingsTransactionCoordinator(
                    file_store, secret_store, ReferenceFactory()
                ).save(current, current, {"llm.apiKey": self.secret})

            self.assert_error_redacted(raised.exception)
            journal_payload = file_store.paths.journal_file.read_text("utf-8")
            settings_payload = file_store.paths.settings_file.read_text("utf-8")
            json.loads(journal_payload)
            json.loads(settings_payload)
            self.assertNotIn(self.secret, journal_payload)
            self.assertNotIn(self.secret, settings_payload)


if __name__ == "__main__":
    unittest.main()
