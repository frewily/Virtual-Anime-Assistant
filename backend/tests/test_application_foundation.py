import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.assistant import AssistantApplication
from application.events import ResponsePublisher
from application.sessions import SessionRegistry
from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    MessageSource,
    ScenarioContent,
    SenderIdentity,
)
from domain.responses import AssistantResponse, ResponseKind


def message(conversation_id: str = "desktop:user-1") -> IncomingMessage:
    return IncomingMessage(
        conversation_id=conversation_id,
        source=MessageSource.DESKTOP,
        sender=SenderIdentity(id="user-1"),
        content=ChatContent(text="你好"),
    )


def response_for(item: IncomingMessage) -> AssistantResponse:
    return AssistantResponse(
        correlation_id=item.message_id,
        conversation_id=item.conversation_id,
        kind=ResponseKind.SPEAK,
        text="收到",
    )


class ApplicationFoundationTests(unittest.TestCase):
    def test_publisher_isolates_failed_subscribers(self):
        publisher = ResponsePublisher()
        failed = AsyncMock(side_effect=RuntimeError("offline"))
        healthy = AsyncMock()
        publisher.subscribe(failed)
        publisher.subscribe(healthy)
        item = message()

        asyncio.run(publisher.publish(response_for(item)))

        failed.assert_awaited_once()
        healthy.assert_awaited_once()

    def test_unsubscribe_stops_delivery(self):
        publisher = ResponsePublisher()
        subscriber = AsyncMock()
        unsubscribe = publisher.subscribe(subscriber)
        unsubscribe()

        asyncio.run(publisher.publish(response_for(message())))

        subscriber.assert_not_awaited()

    def test_same_conversation_is_processed_sequentially(self):
        registry = SessionRegistry()
        active = 0
        max_active = 0

        async def handler(item, state):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return response_for(item)

        async def run_both():
            await asyncio.gather(
                registry.run(message(), handler),
                registry.run(message(), handler),
            )

        asyncio.run(run_both())

        self.assertEqual(max_active, 1)
        self.assertEqual(registry.get_state("desktop:user-1").turn_count, 2)


class AssistantApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tts = AsyncMock()
        self.tts.synthesize.return_value = {
            "audio_url": "/api/tts/audio/example.wav",
            "text": "电脑好热啊",
        }
        self.publisher = ResponsePublisher()
        self.subscriber = AsyncMock()
        self.publisher.subscribe(self.subscriber)
        self.application = AssistantApplication(
            tts=self.tts,
            publisher=self.publisher,
        )

    def test_chat_returns_compatible_fixed_reply(self):
        result = asyncio.run(self.application.handle(message()))

        self.assertEqual(result.kind, ResponseKind.SPEAK)
        self.assertEqual(result.text, "主人说得有道理~")
        self.subscriber.assert_awaited_once_with(result)

    def test_interaction_returns_avatar_action(self):
        item = message()
        item.content = InteractionContent(action="click", x=10, y=20)

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.kind, ResponseKind.ACTION)
        self.assertEqual(result.avatar.motion, "tap_body")

    def test_scenario_synthesizes_audio(self):
        item = IncomingMessage(
            conversation_id="scenario:high_cpu",
            source=MessageSource.SCENARIO,
            sender=SenderIdentity(id="scenario-engine"),
            content=ScenarioContent(
                scenario_id="high_cpu",
                text="电脑好热啊",
                expression="worried",
                motion="shake",
            ),
        )

        result = asyncio.run(self.application.handle(item))

        self.tts.synthesize.assert_awaited_once_with("电脑好热啊")
        self.assertEqual(result.audio_url, "/api/tts/audio/example.wav")
        self.assertEqual(result.avatar.expression, "worried")
