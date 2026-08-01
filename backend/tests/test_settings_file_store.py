"""Tests for strict, atomic local settings-file persistence."""

import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.file_store import SaveJournal, SettingsFileError, SettingsFileStore
from settings.models import LLMSettings, PersistedSettings
from settings.paths import SettingsPaths


class SettingsFileStoreTests(unittest.TestCase):
    def make_store(self, root: Path) -> SettingsFileStore:
        return SettingsFileStore(SettingsPaths.from_root(root))

    def assert_error_chain_does_not_contain(
        self, error: BaseException, secret: str
    ) -> None:
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertNotIn(secret, "".join(traceback.format_exception(error)))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_load_from_empty_directory_returns_defaults_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = SettingsPaths.from_root(Path(temporary_directory))

            loaded = SettingsFileStore(paths).load()

            self.assertEqual(loaded, PersistedSettings())
            self.assertFalse(paths.settings_file.exists())

    def test_save_round_trips_camel_case_json_without_plaintext_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            settings = PersistedSettings(
                llm=LLMSettings(base_url="https://llm.example", api_key_ref="keyring:llm")
            )

            store.save(settings)

            raw_json = store.paths.settings_file.read_text(encoding="utf-8")
            self.assertEqual(store.load(), settings)
            self.assertEqual(json.loads(raw_json)["schemaVersion"], 1)
            self.assertNotIn('"apiKey":', raw_json)
            self.assertNotIn('"accessToken":', raw_json)

    def test_invalid_settings_files_raise_without_changing_original_bytes(self) -> None:
        invalid_payloads = (
            b"{not json",
            b'{"schemaVersion":99}',
            b'{"schemaVersion":1,"unexpected":true}',
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary_directory:
                store = self.make_store(Path(temporary_directory))
                store.paths.settings_file.write_bytes(payload)

                with self.assertRaises(SettingsFileError):
                    store.load()

                self.assertEqual(store.paths.settings_file.read_bytes(), payload)

    def test_invalid_settings_payload_is_not_retained_in_error_chain(self) -> None:
        secret = "private-sentinel"
        payload = (
            b'{"schemaVersion":1,"llm":{"enabled":true,'
            b'"unexpected":"private-sentinel"}}'
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            store.paths.settings_file.write_bytes(payload)

            with self.assertRaises(SettingsFileError) as raised:
                store.load()

            self.assert_error_chain_does_not_contain(raised.exception, secret)

    def test_load_does_not_treat_permission_error_as_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))

            with (
                patch.object(Path, "exists", side_effect=AssertionError("exists called")),
                patch.object(Path, "read_bytes", side_effect=PermissionError),
                self.assertRaises(SettingsFileError),
            ):
                store.load()

    def test_failed_replace_preserves_existing_settings_and_cleans_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            original = PersistedSettings()
            store.save(original)
            replacement = PersistedSettings(llm=LLMSettings(enabled=True))

            with patch("os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(SettingsFileError):
                    store.save(replacement)

            self.assertEqual(store.load(), original)
            self.assertEqual(
                sorted(entry.name for entry in store.paths.root.iterdir()),
                ["settings.json"],
            )

    def test_keyboard_interrupt_cleans_temporary_file_without_wrapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))

            with patch("os.fsync", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    store.save(PersistedSettings())

            self.assertEqual(list(store.paths.root.iterdir()), [])

    def test_journal_round_trip_and_delete_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            journal = SaveJournal(
                transaction_id="save-123",
                old_refs=["keyring:old"],
                new_refs=["keyring:new"],
            )

            store.write_journal(journal)

            self.assertEqual(store.read_journal(), journal)
            store.delete_journal()
            self.assertIsNone(store.read_journal())
            store.delete_journal()

    def test_invalid_journals_raise_settings_file_error(self) -> None:
        invalid_payloads = (
            b"{not json",
            b'{"schemaVersion":99,"transactionId":"save-123","oldRefs":[],"newRefs":[]}',
            b'{"schemaVersion":1,"transactionId":"save-123","oldRefs":[],"newRefs":[],"extra":true}',
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary_directory:
                store = self.make_store(Path(temporary_directory))
                store.paths.journal_file.write_bytes(payload)

                with self.assertRaises(SettingsFileError):
                    store.read_journal()

                self.assertEqual(store.paths.journal_file.read_bytes(), payload)

    def test_invalid_journal_payload_is_not_retained_in_error_chain(self) -> None:
        secret = "private-sentinel"
        payload = (
            b'{"schemaVersion":1,"transactionId":"save-123",'
            b'"oldRefs":[],"newRefs":[],"unexpected":"private-sentinel"}'
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            store.paths.journal_file.write_bytes(payload)

            with self.assertRaises(SettingsFileError) as raised:
                store.read_journal()

            self.assert_error_chain_does_not_contain(raised.exception, secret)

    def test_read_journal_does_not_treat_permission_error_as_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))

            with (
                patch.object(Path, "exists", side_effect=AssertionError("exists called")),
                patch.object(Path, "read_bytes", side_effect=PermissionError),
                self.assertRaises(SettingsFileError),
            ):
                store.read_journal()

    def test_outer_exception_context_is_suppressed(self) -> None:
        outer_secret = "outer-private-sentinel"
        file_secret = "file-private-sentinel"
        payload = (
            b'{"schemaVersion":1,"llm":{"enabled":true,'
            b'"unexpected":"file-private-sentinel"}}'
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))
            store.paths.settings_file.write_bytes(payload)

            try:
                raise ValueError(outer_secret)
            except ValueError:
                with self.assertRaises(SettingsFileError) as raised:
                    store.load()

            rendered = "".join(traceback.format_exception(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(outer_secret, rendered)
            self.assertNotIn(file_secret, rendered)

    def test_successful_atomic_writes_leave_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self.make_store(Path(temporary_directory))

            store.save(PersistedSettings())
            store.write_journal(
                SaveJournal(transaction_id="save-123", old_refs=[], new_refs=[])
            )

            self.assertEqual(
                sorted(entry.name for entry in store.paths.root.iterdir()),
                ["settings.json", "settings.save-journal.json"],
            )


if __name__ == "__main__":
    unittest.main()
