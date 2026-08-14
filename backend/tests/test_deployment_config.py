import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.deployment import DeploymentSettings


class DeploymentSettingsTests(unittest.TestCase):
    def test_cloud_profile_disables_desktop_monitor(self):
        settings = DeploymentSettings.from_env(
            {"ASSISTANT_RUNTIME_PROFILE": "cloud"}
        )

        self.assertEqual(settings.profile, "cloud")
        self.assertFalse(settings.desktop_monitor_enabled)

    def test_desktop_profile_is_the_default(self):
        settings = DeploymentSettings.from_env({})

        self.assertEqual(settings.profile, "desktop")
        self.assertTrue(settings.desktop_monitor_enabled)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "^invalid runtime profile$"):
            DeploymentSettings.from_env(
                {"ASSISTANT_RUNTIME_PROFILE": "production-ish"}
            )

    def test_cloud_monitor_state_path_has_safe_default_and_override(self):
        default = DeploymentSettings.from_env(
            {"ASSISTANT_RUNTIME_PROFILE": "cloud"}
        )
        overridden = DeploymentSettings.from_env(
            {
                "ASSISTANT_RUNTIME_PROFILE": "cloud",
                "ASSISTANT_CLOUD_MONITOR_STATE_FILE": "/tmp/monitor.json",
            }
        )

        self.assertEqual(
            default.cloud_monitor_state_file,
            Path("/data/operations/cloud-monitor-state.json"),
        )
        self.assertEqual(
            overridden.cloud_monitor_state_file,
            Path("/tmp/monitor.json"),
        )


if __name__ == "__main__":
    unittest.main()
