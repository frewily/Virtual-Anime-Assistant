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

    def test_direct_construction_strictly_validates_security_fields(self):
        invalid_cases = (
            ({"profile": 1, "desktop_monitor_enabled": True}, TypeError),
            ({"profile": "other", "desktop_monitor_enabled": True}, ValueError),
            ({"profile": "desktop", "desktop_monitor_enabled": 1}, TypeError),
            (
                {
                    "profile": "cloud",
                    "desktop_monitor_enabled": False,
                    "computer_default_device_id": "../mac",
                },
                ValueError,
            ),
            (
                {
                    "profile": "cloud",
                    "desktop_monitor_enabled": False,
                    "computer_state_report_token": 123,
                },
                TypeError,
            ),
        )
        for kwargs, exception_type in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(exception_type):
                    DeploymentSettings(**kwargs)

    def test_report_token_is_bounded_visible_ascii_or_explicitly_disabled(self):
        valid = "x" * 32
        enabled = DeploymentSettings(
            profile="cloud",
            desktop_monitor_enabled=False,
            computer_state_report_token=valid,
        )
        disabled = DeploymentSettings(
            profile="cloud",
            desktop_monitor_enabled=False,
        )

        self.assertTrue(enabled.computer_state_report_enabled)
        self.assertFalse(disabled.computer_state_report_enabled)
        for token in (
            "x" * 31,
            "x" * 257,
            "x" * 31 + "\n",
            "x" * 31 + "猫",
        ):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    DeploymentSettings(
                        profile="cloud",
                        desktop_monitor_enabled=False,
                        computer_state_report_token=token,
                    )

    def test_from_env_uses_same_strict_validation(self):
        for environ, exception_type in (
            (
                {
                    "ASSISTANT_RUNTIME_PROFILE": "cloud",
                    "ASSISTANT_COMPUTER_STATE_REPORT_TOKEN": "short",
                },
                ValueError,
            ),
            (
                {
                    "ASSISTANT_RUNTIME_PROFILE": "cloud",
                    "ASSISTANT_COMPUTER_DEVICE_ID": 123,
                },
                TypeError,
            ),
        ):
            with self.subTest(environ=environ):
                with self.assertRaises(exception_type):
                    DeploymentSettings.from_env(environ)


if __name__ == "__main__":
    unittest.main()
