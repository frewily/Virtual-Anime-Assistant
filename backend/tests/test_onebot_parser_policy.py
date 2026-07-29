import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.onebot.config import OneBotSettings
from channels.onebot.models import ParsedOneBotMessage
from channels.onebot.parser import parse_onebot_event
from channels.onebot.policy import (
    AdmissionOutcome,
    OneBotAdmissionPolicy,
    RecentMessageRegistry,
    SenderRateLimiter,
)


def private_event(message: object = " 你好 ") -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 123,
        "user_id": 456,
        "message_id": 10,
        "message": message,
    }


def group_event(message: object | None = None) -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "user_id": 456,
        "group_id": 789,
        "message_id": 11,
        "message": (
            [
                {"type": "at", "data": {"qq": "123"}},
                {"type": "text", "data": {"text": " 你好 "}},
            ]
            if message is None
            else message
        ),
    }


def parsed_message(
    message_type: str = "private",
    *,
    user_id: int = 456,
    group_id: int | None = None,
    mentioned_bot: bool = False,
) -> ParsedOneBotMessage:
    return ParsedOneBotMessage(
        self_id=123,
        user_id=user_id,
        message_id=10,
        message_type=message_type,
        group_id=group_id,
        text="你好",
        mentioned_bot=mentioned_bot,
    )


def policy_settings(
    *,
    rate_per_minute: int = 10,
    rate_burst: int = 2,
) -> OneBotSettings:
    return OneBotSettings(
        enabled=True,
        access_token="0123456789abcdef",
        allowed_group_ids=frozenset({789}),
        allowed_user_ids=frozenset({456}),
        rate_per_minute=rate_per_minute,
        rate_burst=rate_burst,
        max_concurrency=4,
        action_timeout_seconds=10,
    )


class MutableClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class OneBotParserTests(unittest.TestCase):
    def test_private_string_is_plain_text(self):
        parsed = parse_onebot_event(private_event(), expected_self_id=123)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.message_type, "private")
        self.assertIsNone(parsed.group_id)
        self.assertFalse(parsed.mentioned_bot)

    def test_private_array_joins_text_and_ignores_rich_media(self):
        payload = private_event(
            [
                {"type": "text", "data": {"text": "你"}},
                {
                    "type": "image",
                    "data": {
                        "url": "https://private.example/image",
                        "file": "secret",
                    },
                },
                {"type": "text", "data": {"text": "好"}},
            ]
        )

        parsed = parse_onebot_event(payload, expected_self_id=123)

        self.assertEqual(parsed.text, "你好")

    def test_group_array_requires_structured_bot_mention(self):
        parsed = parse_onebot_event(group_event(), expected_self_id=123)

        self.assertEqual(parsed.text, "你好")
        self.assertTrue(parsed.mentioned_bot)
        self.assertEqual(parsed.stable_message_id, "qq:123:11")
        self.assertEqual(
            parsed.conversation_id,
            "qq:group:789:user:456",
        )

    def test_group_string_cq_code_is_ignored(self):
        parsed = parse_onebot_event(
            group_event("[CQ:at,qq=123] 你好"),
            expected_self_id=123,
        )

        self.assertIsNone(parsed)

    def test_group_without_bot_mention_is_parsed_for_policy(self):
        parsed = parse_onebot_event(
            group_event(
                [
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": "你好"}},
                ]
            ),
            expected_self_id=123,
        )

        self.assertEqual(parsed.text, "你好")
        self.assertFalse(parsed.mentioned_bot)

    def test_empty_text_after_removing_mentions_is_ignored(self):
        parsed = parse_onebot_event(
            group_event([{"type": "at", "data": {"qq": "123"}}]),
            expected_self_id=123,
        )

        self.assertIsNone(parsed)

    def test_non_message_self_message_and_wrong_account_are_ignored(self):
        payloads = []
        notice = private_event()
        notice["post_type"] = "notice"
        payloads.append(notice)
        self_message = private_event()
        self_message["user_id"] = 123
        payloads.append(self_message)
        wrong_account = private_event()
        wrong_account["self_id"] = 999
        payloads.append(wrong_account)

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(
                    parse_onebot_event(payload, expected_self_id=123)
                )

    def test_invalid_positive_identifiers_are_ignored(self):
        invalid_payloads = []
        for field, value in (
            ("self_id", True),
            ("user_id", 0),
            ("message_id", -1),
            ("group_id", "789"),
        ):
            payload = group_event()
            payload[field] = value
            invalid_payloads.append(payload)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(
                    parse_onebot_event(payload, expected_self_id=123)
                )

    def test_private_chat_ignores_a_forged_group_id(self):
        payload = private_event()
        payload["group_id"] = "not-an-id"

        parsed = parse_onebot_event(payload, expected_self_id=123)

        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.group_id)

    def test_text_over_4000_characters_is_ignored(self):
        parsed = parse_onebot_event(
            private_event("x" * 4001),
            expected_self_id=123,
        )

        self.assertIsNone(parsed)


class OneBotAdmissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.settings = policy_settings()
        self.limiter = SenderRateLimiter(
            rate_per_minute=self.settings.rate_per_minute,
            burst=self.settings.rate_burst,
            clock=self.clock,
        )
        self.policy = OneBotAdmissionPolicy(
            self.settings,
            self.limiter,
        )

    def test_private_chat_only_requires_user_allowlist(self):
        outcome = self.policy.admit(parsed_message())

        self.assertIs(outcome, AdmissionOutcome.ALLOW)

    def test_group_chat_requires_group_allowlist_and_bot_mention(self):
        mentioned = parsed_message(
            "group",
            group_id=789,
            mentioned_bot=True,
        )
        not_mentioned = parsed_message(
            "group",
            group_id=789,
            mentioned_bot=False,
        )
        other_group = parsed_message(
            "group",
            group_id=999,
            mentioned_bot=True,
        )

        self.assertIs(
            self.policy.admit(mentioned),
            AdmissionOutcome.ALLOW,
        )
        self.assertIs(
            self.policy.admit(not_mentioned),
            AdmissionOutcome.IGNORE,
        )
        self.assertIs(
            self.policy.admit(other_group),
            AdmissionOutcome.IGNORE,
        )

    def test_group_membership_does_not_grant_private_access(self):
        outsider = parsed_message(user_id=999)
        group_member = parsed_message(
            "group",
            user_id=999,
            group_id=789,
            mentioned_bot=True,
        )

        self.assertIs(
            self.policy.admit(outsider),
            AdmissionOutcome.IGNORE,
        )
        self.assertIs(
            self.policy.admit(group_member),
            AdmissionOutcome.ALLOW,
        )

    def test_private_and_group_messages_share_the_sender_bucket(self):
        settings = policy_settings(rate_per_minute=1, rate_burst=1)
        limiter = SenderRateLimiter(
            rate_per_minute=1,
            burst=1,
            clock=self.clock,
        )
        policy = OneBotAdmissionPolicy(settings, limiter)

        self.assertIs(
            policy.admit(parsed_message()),
            AdmissionOutcome.ALLOW,
        )
        self.assertIs(
            policy.admit(
                parsed_message(
                    "group",
                    group_id=789,
                    mentioned_bot=True,
                )
            ),
            AdmissionOutcome.RATE_LIMITED,
        )

    def test_bucket_allows_burst_then_refills_at_configured_rate(self):
        settings = policy_settings(rate_per_minute=60, rate_burst=2)
        limiter = SenderRateLimiter(
            rate_per_minute=60,
            burst=2,
            clock=self.clock,
        )
        policy = OneBotAdmissionPolicy(settings, limiter)
        message = parsed_message()

        self.assertIs(policy.admit(message), AdmissionOutcome.ALLOW)
        self.assertIs(policy.admit(message), AdmissionOutcome.ALLOW)
        self.assertIs(
            policy.admit(message),
            AdmissionOutcome.RATE_LIMITED,
        )
        self.clock.advance(1)
        self.assertIs(policy.admit(message), AdmissionOutcome.ALLOW)

    def test_idle_buckets_are_removed_without_changing_active_limits(self):
        message = parsed_message()
        self.policy.admit(message)
        self.assertEqual(self.limiter.tracked_sender_count, 1)

        self.clock.advance(601)
        self.limiter.prune()

        self.assertEqual(self.limiter.tracked_sender_count, 0)
        self.assertIs(
            self.policy.admit(message),
            AdmissionOutcome.ALLOW,
        )

    def test_recent_message_registry_rejects_concurrent_and_recent_duplicates(
        self,
    ):
        registry = RecentMessageRegistry(clock=self.clock)

        self.assertTrue(registry.claim("qq:123:10"))
        self.assertFalse(registry.claim("qq:123:10"))
        self.clock.advance(601)
        self.assertTrue(registry.claim("qq:123:10"))

    def test_failed_preprocessing_can_release_a_claim_for_redelivery(self):
        registry = RecentMessageRegistry(clock=self.clock)
        self.assertTrue(registry.claim("qq:123:10"))

        registry.release("qq:123:10")

        self.assertTrue(registry.claim("qq:123:10"))

    def test_recent_registry_enforces_its_size_limit(self):
        registry = RecentMessageRegistry(
            clock=self.clock,
            max_entries=2,
        )
        registry.claim("qq:123:1")
        self.clock.advance(1)
        registry.claim("qq:123:2")
        self.clock.advance(1)
        registry.claim("qq:123:3")

        self.assertTrue(registry.claim("qq:123:1"))
        self.assertFalse(registry.claim("qq:123:3"))


if __name__ == "__main__":
    unittest.main()
