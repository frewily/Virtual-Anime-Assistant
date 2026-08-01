"""Tests for strict persisted settings models and settings paths."""

from pathlib import Path
import sys
import unittest

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.models import PersistedSettings, SecretMutation
from settings.paths import SettingsPaths


class PersistedSettingsTests(unittest.TestCase):
    def test_defaults_match_settings_specification(self) -> None:
        settings = PersistedSettings()

        self.assertEqual(settings.schema_version, 1)
        self.assertEqual(settings.llm.timeout_seconds, 60)
        self.assertEqual(settings.qq.rate_per_minute, 10)
        self.assertEqual(settings.tts.audio_max_age_seconds, 86400)

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            PersistedSettings.model_validate(
                {"schemaVersion": 1, "llm": {"apiKey": "plaintext"}}
            )


class SecretMutationTests(unittest.TestCase):
    def test_replace_requires_value(self) -> None:
        with self.assertRaises(ValidationError):
            SecretMutation(operation="replace")

    def test_secret_is_hidden_from_representation(self) -> None:
        mutation = SecretMutation(operation="replace", value="private-key")

        self.assertNotIn("private-key", repr(mutation))


class SettingsPathsTests(unittest.TestCase):
    def test_from_root_builds_settings_and_journal_paths(self) -> None:
        paths = SettingsPaths.from_root(Path("/tmp/example-settings"))

        self.assertEqual(paths.settings_file, Path("/tmp/example-settings/settings.json"))
        self.assertEqual(
            paths.journal_file,
            Path("/tmp/example-settings/settings.save-journal.json"),
        )


if __name__ == "__main__":
    unittest.main()
