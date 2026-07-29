import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.onebot.channel import (
    OneBotChannel,
    group_reply_action,
    private_reply_action,
    split_reply,
    to_incoming_message,
)
from channels.onebot.config import OneBotSettings
from channels.onebot.models import (
    ONEBOT_ACTION_TIMEOUT,
    OneBotChannelError,
    ParsedOneBotMessage,
)
from domain.messages import MessageSource
from domain.responses import (
    AssistantResponse,
    AvatarCue,
    ResponseKind,
)


def settings(
    *,
    users: frozenset[int] = frozenset({456, 457, 458}),
    groups: frozenset[int] = frozenset({789}),
    rate_per_minute: int = 120,
    rate_burst: int = 20,
    max_concurrency: int = 4,
) -> OneBotSettings:
    return OneBotSettings(
        enabled=True,
        access_token="0123456789abcdef",
        allowed_group_ids=groups,
        allowed_user_ids=users,
        rate_per_minute=rate_per_minute,
        rate_burst=rate_burst,
        max_concurrency=max_concurrency,
        action_timeout_seconds=10,
    )


def private_payload(
    *,
    user_id: int = 456,
    message_id: int = 10,
    text: str = "你好",
) -> dict[str, object]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 123,
        "user_id": user_id,
        "message_id": message_id,
        "message": text,
    }


def group_payload(
    *,
    user_id: int = 456,
    group_id: int = 789,
    message_id: int = 11,
    mention: bool = True,
) -> dict[str, object]:
    message: list[object] = []
    if mention:
        message.append({"type": "at", "data": {"qq": "123"}})
    message.append({"type": "text", "data": {"text": "你好"}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": message_id,
        "message": message,
    }


def parsed_private() -> ParsedOneBotMessage:
    return ParsedOneBotMessage(
        self_id=123,
        user_id=456,
        message_id=10,
        message_type="private",
        group_id=None,
        text="你好",
        mentioned_bot=False,
    )


def parsed_group() -> ParsedOneBotMessage:
    return ParsedOneBotMessage(
        self_id=123,
        user_id=456,
        message_id=11,
        message_type="group",
        group_id=789,
        text="你好",
        mentioned_bot=True,
    )


def response_for(
    message_id: str,
    conversation_id: str,
    *,
    text: str | None = "收到",
    kind: ResponseKind = ResponseKind.SPEAK,
) -> AssistantResponse:
    return AssistantResponse(
        correlation_id=message_id,
        conversation_id=conversation_id,
        kind=kind,
        text=text,
        avatar=(
            AvatarCue(motion="wave")
            if kind is ResponseKind.ACTION
            else None
        ),
    )


class FakeApplication:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.messages = []
        self.process = AsyncMock(side_effect=self._process)
        self.has_seen_message = AsyncMock(side_effect=self._has_seen)

    async def _has_seen(self, message_id: str) -> bool:
        return message_id in self.seen

    async def _process(self, message):
        self.messages.append(message)
        self.seen.add(message.message_id)
        return response_for(
            message.message_id,
            message.conversation_id,
        )


class FakeConnection:
    def __init__(self) -> None:
        self.actions = []
        self.send_action = AsyncMock(side_effect=self._send)

    async def _send(self, action) -> None:
        self.actions.append(action)


class OneBotChannelConversionTests(unittest.TestCase):
    def test_private_message_maps_to_unified_qq_message(self):
        incoming = to_incoming_message(parsed_private())

        self.assertEqual(incoming.message_id, "qq:123:10")
        self.assertEqual(incoming.conversation_id, "qq:private:456")
        self.assertIs(incoming.source, MessageSource.QQ)
        self.assertEqual(incoming.sender.id, "456")
        self.assertEqual(incoming.content.text, "你好")
        self.assertEqual(
            incoming.metadata,
            {
                "self_id": 123,
                "user_id": 456,
                "message_id": 10,
            },
        )

    def test_group_message_isolates_user_and_group_metadata(self):
        incoming = to_incoming_message(parsed_group())

        self.assertEqual(
            incoming.conversation_id,
            "qq:group:789:user:456",
        )
        self.assertEqual(
            incoming.metadata,
            {
                "self_id": 123,
                "user_id": 456,
                "message_id": 11,
                "group_id": 789,
            },
        )

    def test_private_reply_uses_text_message_array(self):
        action = private_reply_action(parsed_private(), "收到")

        self.assertEqual(action.action, "send_private_msg")
        self.assertEqual(
            action.params,
            {
                "user_id": 456,
                "message": [
                    {"type": "text", "data": {"text": "收到"}}
                ],
            },
        )

    def test_first_group_reply_quotes_and_mentions_sender(self):
        first = group_reply_action(
            parsed_group(),
            "第一段",
            first_chunk=True,
        )
        later = group_reply_action(
            parsed_group(),
            "第二段",
            first_chunk=False,
        )

        self.assertEqual(first.action, "send_group_msg")
        self.assertEqual(first.params["group_id"], 789)
        self.assertEqual(
            first.params["message"],
            [
                {"type": "reply", "data": {"id": "11"}},
                {"type": "at", "data": {"qq": "456"}},
                {"type": "text", "data": {"text": "第一段"}},
            ],
        )
        self.assertEqual(
            later.params["message"],
            [{"type": "text", "data": {"text": "第二段"}}],
        )

    def test_long_reply_is_split_at_4000_character_boundaries(self):
        chunks = split_reply("x" * 9001)

        self.assertEqual([len(chunk) for chunk in chunks], [4000, 4000, 1001])
        self.assertEqual("".join(chunks), "x" * 9001)


class OneBotChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.application = FakeApplication()
        self.connection = FakeConnection()
        self.channel = OneBotChannel(
            application=self.application,
            settings=settings(),
            connection=self.connection,
        )

    async def test_private_event_processes_without_a_handle_method(self):
        self.assertFalse(hasattr(self.application, "handle"))

        await self.channel.handle_event(
            private_payload(),
            self_id=123,
        )

        self.application.process.assert_awaited_once()
        incoming = self.application.messages[0]
        self.assertEqual(incoming.conversation_id, "qq:private:456")
        self.assertEqual(len(self.connection.actions), 1)
        self.assertEqual(
            self.connection.actions[0].action,
            "send_private_msg",
        )

    async def test_group_event_sends_quote_mention_and_text(self):
        await self.channel.handle_event(
            group_payload(),
            self_id=123,
        )

        self.application.process.assert_awaited_once()
        action = self.connection.actions[0]
        self.assertEqual(action.action, "send_group_msg")
        self.assertEqual(
            [segment["type"] for segment in action.params["message"]],
            ["reply", "at", "text"],
        )

    async def test_unauthorized_or_unmentioned_events_are_silent(self):
        payloads = (
            private_payload(user_id=999, message_id=20),
            group_payload(group_id=999, message_id=21),
            group_payload(mention=False, message_id=22),
        )

        for payload in payloads:
            await self.channel.handle_event(payload, self_id=123)

        self.application.process.assert_not_awaited()
        self.connection.send_action.assert_not_awaited()

    async def test_rate_limited_event_does_not_process_or_reply(self):
        channel = OneBotChannel(
            application=self.application,
            settings=settings(
                rate_per_minute=1,
                rate_burst=1,
            ),
            connection=self.connection,
        )

        await channel.handle_event(
            private_payload(message_id=30),
            self_id=123,
        )
        await channel.handle_event(
            private_payload(message_id=31),
            self_id=123,
        )

        self.assertEqual(self.application.process.await_count, 1)
        self.assertEqual(self.connection.send_action.await_count, 1)

    async def test_recent_and_persisted_duplicates_are_silent(self):
        await self.channel.handle_event(
            private_payload(message_id=40),
            self_id=123,
        )
        await self.channel.handle_event(
            private_payload(message_id=40),
            self_id=123,
        )
        self.application.seen.add("qq:123:41")
        await self.channel.handle_event(
            private_payload(message_id=41),
            self_id=123,
        )

        self.assertEqual(self.application.process.await_count, 1)
        self.assertEqual(self.connection.send_action.await_count, 1)

    async def test_concurrent_duplicate_only_enters_application_once(self):
        gate = asyncio.Event()

        async def blocked_process(message):
            self.application.messages.append(message)
            await gate.wait()
            return response_for(
                message.message_id,
                message.conversation_id,
            )

        self.application.process.side_effect = blocked_process
        first = asyncio.create_task(
            self.channel.handle_event(
                private_payload(message_id=50),
                self_id=123,
            )
        )
        second = asyncio.create_task(
            self.channel.handle_event(
                private_payload(message_id=50),
                self_id=123,
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(self.application.process.await_count, 1)
        gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.connection.send_action.await_count, 1)

    async def test_failed_seen_check_releases_recent_claim(self):
        self.application.has_seen_message.side_effect = [
            RuntimeError("temporary store failure"),
            False,
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "temporary store failure",
        ):
            await self.channel.handle_event(
                private_payload(message_id=60),
                self_id=123,
            )
        await self.channel.handle_event(
            private_payload(message_id=60),
            self_id=123,
        )

        self.application.process.assert_awaited_once()

    async def test_process_failure_keeps_recent_claim(self):
        self.application.process.side_effect = RuntimeError(
            "application failure"
        )

        with self.assertRaisesRegex(RuntimeError, "application failure"):
            await self.channel.handle_event(
                private_payload(message_id=61),
                self_id=123,
            )
        await self.channel.handle_event(
            private_payload(message_id=61),
            self_id=123,
        )

        self.application.process.assert_awaited_once()

    async def test_action_and_blank_status_responses_are_not_sent(self):
        responses = (
            response_for(
                "qq:123:70",
                "qq:private:456",
                text=None,
                kind=ResponseKind.ACTION,
            ),
            response_for(
                "qq:123:71",
                "qq:private:456",
                text="   ",
                kind=ResponseKind.STATUS,
            ),
        )
        self.application.process.side_effect = responses

        await self.channel.handle_event(
            private_payload(message_id=70),
            self_id=123,
        )
        await self.channel.handle_event(
            private_payload(message_id=71),
            self_id=123,
        )

        self.connection.send_action.assert_not_awaited()

    async def test_safe_error_text_is_sent_as_a_normal_reply(self):
        self.application.process.return_value = response_for(
            "qq:123:72",
            "qq:private:456",
            text="模型服务暂时不可用，请稍后再试。",
            kind=ResponseKind.ERROR,
        )
        self.application.process.side_effect = None

        await self.channel.handle_event(
            private_payload(message_id=72),
            self_id=123,
        )

        self.connection.send_action.assert_awaited_once()
        self.assertEqual(
            self.connection.actions[0].params["message"][0]["data"]["text"],
            "模型服务暂时不可用，请稍后再试。",
        )

    async def test_send_timeout_isolated_from_next_event(self):
        self.connection.send_action.side_effect = [
            OneBotChannelError(ONEBOT_ACTION_TIMEOUT),
            None,
        ]

        await self.channel.handle_event(
            private_payload(message_id=80),
            self_id=123,
        )
        await self.channel.handle_event(
            private_payload(message_id=81),
            self_id=123,
        )

        self.assertEqual(self.application.process.await_count, 2)
        self.assertEqual(self.connection.send_action.await_count, 2)

    async def test_global_concurrency_limit_applies_across_qq_sessions(self):
        application = FakeApplication()
        active = 0
        maximum_active = 0
        gate = asyncio.Event()

        async def concurrent_process(message):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await gate.wait()
            active -= 1
            return response_for(
                message.message_id,
                message.conversation_id,
            )

        application.process.side_effect = concurrent_process
        channel = OneBotChannel(
            application=application,
            settings=settings(max_concurrency=2),
            connection=self.connection,
        )
        tasks = [
            asyncio.create_task(
                channel.handle_event(
                    private_payload(
                        user_id=user_id,
                        message_id=message_id,
                    ),
                    self_id=123,
                )
            )
            for user_id, message_id in ((456, 90), (457, 91), (458, 92))
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(maximum_active, 2)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(application.process.await_count, 3)


if __name__ == "__main__":
    unittest.main()
