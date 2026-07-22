import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.events import ResponsePublisher
from application.sessions import SessionRegistry
from domain.messages import ChatContent, IncomingMessage, MessageSource, SenderIdentity
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
