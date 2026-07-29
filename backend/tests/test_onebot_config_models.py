import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.onebot.config import OneBotSettings
from channels.onebot.models import (
    ONEBOT_ACTION_FAILED,
    OneBotChannelError,
    ParsedOneBotMessage,
    QQ_MISCONFIGURED,
)


def enabled_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "ASSISTANT_QQ_ENABLED": "true",
        "ASSISTANT_QQ_ACCESS_TOKEN": "0123456789abcdef",
        "ASSISTANT_QQ_ALLOWED_GROUP_IDS": "10001",
        "ASSISTANT_QQ_ALLOWED_USER_IDS": "20001",
    }
    environment.update(overrides)
    return environment


class OneBotConfigTests(unittest.TestCase):
    def test_channel_is_disabled_by_default(self):
        settings = OneBotSettings.from_env({})

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.ready)
        self.assertIsNone(settings.configuration_error)
        self.assertEqual(settings.access_token, "")
        self.assertEqual(settings.allowed_group_ids, frozenset())
        self.assertEqual(settings.allowed_user_ids, frozenset())
        self.assertEqual(settings.rate_per_minute, 10)
        self.assertEqual(settings.rate_burst, 2)
        self.assertEqual(settings.max_concurrency, 4)
        self.assertEqual(settings.action_timeout_seconds, 10)

    def test_enabled_configuration_parses_token_allowlists_and_limits(self):
        settings = OneBotSettings.from_env(
            enabled_environment(
                ASSISTANT_QQ_ACCESS_TOKEN="  0123456789abcdef  ",
                ASSISTANT_QQ_ALLOWED_GROUP_IDS="10001, 10002,10001",
                ASSISTANT_QQ_ALLOWED_USER_IDS="20001, 20002",
                ASSISTANT_QQ_RATE_PER_MINUTE="12",
                ASSISTANT_QQ_RATE_BURST="3",
                ASSISTANT_QQ_MAX_CONCURRENCY="5",
                ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS="8",
            )
        )

        self.assertTrue(settings.enabled)
        self.assertTrue(settings.ready)
        self.assertIsNone(settings.configuration_error)
        self.assertEqual(settings.access_token, "0123456789abcdef")
        self.assertEqual(
            settings.allowed_group_ids,
            frozenset({10001, 10002}),
        )
        self.assertEqual(
            settings.allowed_user_ids,
            frozenset({20001, 20002}),
        )
        self.assertEqual(settings.rate_per_minute, 12)
        self.assertEqual(settings.rate_burst, 3)
        self.assertEqual(settings.max_concurrency, 5)
        self.assertEqual(settings.action_timeout_seconds, 8)
        self.assertNotIn("0123456789abcdef", repr(settings))

    def test_group_and_user_allowlists_remain_independent(self):
        settings = OneBotSettings.from_env(
            enabled_environment(
                ASSISTANT_QQ_ALLOWED_GROUP_IDS="10001",
                ASSISTANT_QQ_ALLOWED_USER_IDS="20001",
            )
        )

        self.assertIn(10001, settings.allowed_group_ids)
        self.assertNotIn(10001, settings.allowed_user_ids)
        self.assertIn(20001, settings.allowed_user_ids)
        self.assertNotIn(20001, settings.allowed_group_ids)

    def test_enabled_channel_requires_a_16_character_token(self):
        settings = OneBotSettings.from_env(
            enabled_environment(ASSISTANT_QQ_ACCESS_TOKEN="too-short")
        )

        self.assertTrue(settings.enabled)
        self.assertFalse(settings.ready)
        self.assertEqual(settings.configuration_error, QQ_MISCONFIGURED)

    def test_enabled_channel_requires_at_least_one_allowlist_entry(self):
        settings = OneBotSettings.from_env(
            enabled_environment(
                ASSISTANT_QQ_ALLOWED_GROUP_IDS="",
                ASSISTANT_QQ_ALLOWED_USER_IDS="",
            )
        )

        self.assertTrue(settings.enabled)
        self.assertFalse(settings.ready)
        self.assertEqual(settings.configuration_error, QQ_MISCONFIGURED)

    def test_invalid_boolean_id_and_numeric_ranges_become_misconfigured(self):
        invalid_environments = (
            enabled_environment(ASSISTANT_QQ_ENABLED="sometimes"),
            enabled_environment(ASSISTANT_QQ_ALLOWED_GROUP_IDS="0"),
            enabled_environment(ASSISTANT_QQ_ALLOWED_USER_IDS="abc"),
            enabled_environment(ASSISTANT_QQ_RATE_PER_MINUTE="0"),
            enabled_environment(ASSISTANT_QQ_RATE_BURST="21"),
            enabled_environment(ASSISTANT_QQ_MAX_CONCURRENCY="33"),
            enabled_environment(ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS="61"),
        )

        for environment in invalid_environments:
            with self.subTest(environment=environment):
                settings = OneBotSettings.from_env(environment)
                self.assertTrue(settings.enabled)
                self.assertFalse(settings.ready)
                self.assertEqual(
                    settings.configuration_error,
                    QQ_MISCONFIGURED,
                )

    def test_burst_cannot_exceed_per_minute_rate(self):
        settings = OneBotSettings.from_env(
            enabled_environment(
                ASSISTANT_QQ_RATE_PER_MINUTE="2",
                ASSISTANT_QQ_RATE_BURST="3",
            )
        )

        self.assertFalse(settings.ready)
        self.assertEqual(settings.configuration_error, QQ_MISCONFIGURED)

    def test_token_is_absent_from_repr_and_configuration_errors(self):
        token = "this-token-must-not-leak"
        settings = OneBotSettings.from_env(
            enabled_environment(
                ASSISTANT_QQ_ACCESS_TOKEN=token,
                ASSISTANT_QQ_ALLOWED_GROUP_IDS="invalid-id",
            )
        )

        self.assertNotIn(token, repr(settings))
        self.assertNotIn(token, settings.configuration_error or "")
        self.assertEqual(settings.configuration_error, QQ_MISCONFIGURED)

    def test_parsed_message_builds_stable_ids(self):
        private = ParsedOneBotMessage(
            self_id=123,
            user_id=456,
            message_id=10,
            message_type="private",
            group_id=None,
            text="你好",
            mentioned_bot=False,
        )
        group = ParsedOneBotMessage(
            self_id=123,
            user_id=456,
            message_id=11,
            message_type="group",
            group_id=789,
            text="你好",
            mentioned_bot=True,
        )

        self.assertEqual(private.stable_message_id, "qq:123:10")
        self.assertEqual(private.conversation_id, "qq:private:456")
        self.assertEqual(group.stable_message_id, "qq:123:11")
        self.assertEqual(group.conversation_id, "qq:group:789:user:456")

    def test_channel_error_only_exposes_a_stable_code(self):
        error = OneBotChannelError(ONEBOT_ACTION_FAILED)

        self.assertEqual(error.code, ONEBOT_ACTION_FAILED)
        self.assertEqual(str(error), ONEBOT_ACTION_FAILED)


if __name__ == "__main__":
    unittest.main()
