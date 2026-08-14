import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.privacy import (
    PrivacyLevel,
    classify_app,
    sanitize_foreground,
)


class ComputerPrivacyTests(unittest.TestCase):
    def test_browser_hides_page_title_and_url(self):
        classification = classify_app("Safari", "com.apple.Safari")
        state = sanitize_foreground(
            "Safari",
            "Bank - Account https://bank.example/private",
            bundle_id="com.apple.Safari",
        )

        self.assertEqual(classification.level, PrivacyLevel.BROWSER)
        self.assertEqual(state.app_name, "Safari")
        self.assertIsNone(state.window_title)

    def test_chat_mail_terminal_and_code_tools_hide_titles(self):
        for app_name in ("QQ", "Mail", "Terminal", "Visual Studio Code"):
            with self.subTest(app=app_name):
                state = sanitize_foreground(app_name, "Sensitive title")
                self.assertEqual(state.privacy_level, PrivacyLevel.HIDE_TITLE)
                self.assertEqual(state.app_name, app_name)
                self.assertIsNone(state.window_title)

    def test_secret_apps_are_fully_anonymized(self):
        for app_name in ("1Password", "Bitwarden", "招商银行", "Authenticator"):
            with self.subTest(app=app_name):
                state = sanitize_foreground(app_name, "Vault")
                self.assertEqual(state.privacy_level, PrivacyLevel.SECRET)
                self.assertEqual(state.app_name, "私密应用")
                self.assertIsNone(state.window_title)

    def test_unknown_apps_default_to_hidden_title(self):
        state = sanitize_foreground("UnknownApp", "Sensitive title")

        self.assertEqual(state.privacy_level, PrivacyLevel.HIDE_TITLE)
        self.assertEqual(state.app_name, "UnknownApp")
        self.assertIsNone(state.window_title)

    def test_show_apps_receive_bounded_control_free_titles(self):
        state = sanitize_foreground(
            "Music",
            "\x00  Song\nName  " + "x" * 200,
            fullscreen=True,
        )

        self.assertEqual(state.privacy_level, PrivacyLevel.SHOW)
        self.assertEqual(state.app_name, "Music")
        self.assertTrue(state.fullscreen)
        self.assertNotIn("\x00", state.window_title)
        self.assertNotIn("\n", state.window_title)
        self.assertLessEqual(len(state.window_title), 128)

    def test_blank_or_untrusted_app_names_fail_closed(self):
        for app_name in ("", "\x00\n", "   "):
            with self.subTest(app=repr(app_name)):
                state = sanitize_foreground(app_name, "Title")
                self.assertEqual(state.privacy_level, PrivacyLevel.SECRET)
                self.assertEqual(state.app_name, "私密应用")
                self.assertIsNone(state.window_title)


if __name__ == "__main__":
    unittest.main()
