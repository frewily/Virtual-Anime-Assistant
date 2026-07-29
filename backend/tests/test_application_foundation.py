import asyncio
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.assistant import AssistantApplication, AssistantStore
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.model_tools import (
    ModelToolLimitError,
    ModelToolOrchestrationError,
)
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
from llm.errors import (
    ModelAuthenticationError,
    ModelGatewayError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from llm.models import (
    ModelAttempt,
    ModelOrchestrationResult,
    ModelReply,
    ModelRequest,
)
from infrastructure.sqlite_store import SqliteStore
from memory.models import (
    MemoryItem,
    MessageStatus,
    ModelCallRecord,
    StoredMessage,
)


def message(
    conversation_id: str = "desktop:user-1",
    *,
    message_id: str = "message-1",
    text: str = "你好",
    source: MessageSource = MessageSource.DESKTOP,
    sender_id: str = "user-1",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        source=source,
        sender=SenderIdentity(id=sender_id),
        content=ChatContent(text=text),
        timestamp=datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc),
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


class FakeLanguageModel:
    def __init__(
        self,
        reply: ModelReply | None = None,
        error: ModelGatewayError | None = None,
    ) -> None:
        self.model_name = "fake-model"
        self.reply = reply or ModelReply(
            text="模型回答",
            model="fake-model-v2",
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=5,
            provider_request_id="provider-request-1",
        )
        self.error = error
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


class FakeStore:
    def __init__(self) -> None:
        self.conversations: dict[str, tuple[str, str]] = {}
        self.messages: dict[str, StoredMessage] = {}
        self.memories: dict[tuple[str, str, str], MemoryItem] = {}
        self.model_calls: list[ModelCallRecord] = []
        self.recent_limits: list[int] = []
        self.saved_model_results: list[
            tuple[list[ModelCallRecord], StoredMessage]
        ] = []

    async def claim_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        existing = self.conversations.get(conversation_id)
        if existing is not None:
            return existing == (source, owner_id)
        self.conversations[conversation_id] = (source, owner_id)
        return True

    async def claim_message(self, item: StoredMessage) -> bool:
        if item.id in self.messages:
            return False
        self.messages[item.id] = item
        return True

    async def save_message(self, item: StoredMessage) -> None:
        if item.id in self.messages:
            raise ValueError("duplicate message")
        self.messages[item.id] = item

    async def find_message(
        self,
        message_id: str,
    ) -> StoredMessage | None:
        return self.messages.get(message_id)

    async def find_assistant_by_correlation(
        self,
        correlation_id: str,
    ) -> StoredMessage | None:
        matches = [
            item
            for item in self.messages.values()
            if item.role == "assistant"
            and item.correlation_id == correlation_id
        ]
        return matches[-1] if matches else None

    async def recent_messages(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]:
        self.recent_limits.append(limit)
        matches = [
            item
            for item in self.messages.values()
            if item.conversation_id == conversation_id
        ]
        return matches[-limit:]

    async def save_memory(self, item: MemoryItem) -> MemoryItem:
        key = (item.source, item.owner_id, item.normalized_content)
        existing = self.memories.get(key)
        if existing is not None:
            existing.content = item.content
            existing.source_message_id = item.source_message_id
            existing.updated_at = item.updated_at
            return existing
        self.memories[key] = item
        return item

    async def list_memories(
        self,
        source: str,
        owner_id: str,
    ) -> list[MemoryItem]:
        return [
            item
            for (item_source, item_owner, _), item in self.memories.items()
            if item_source == source and item_owner == owner_id
        ]

    async def delete_memory_by_content(
        self,
        source: str,
        owner_id: str,
        normalized_content: str,
    ) -> bool:
        key = (source, owner_id, normalized_content)
        return self.memories.pop(key, None) is not None

    async def record_model_call(self, record: ModelCallRecord) -> None:
        self.model_calls.append(record)

    async def save_model_result(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None:
        await self.save_model_results((record,), assistant_message)

    async def save_model_results(
        self,
        records: Sequence[ModelCallRecord],
        assistant_message: StoredMessage,
    ) -> None:
        batch = [
            record.model_copy(deep=True)
            for record in records
        ]
        if not batch:
            raise ValueError("at least one model call is required")
        assistant_snapshot = assistant_message.model_copy(deep=True)

        existing_ids = {record.id for record in self.model_calls}
        batch_ids = [record.id for record in batch]
        if (
            assistant_snapshot.id in self.messages
            or existing_ids.intersection(batch_ids)
            or len(set(batch_ids)) != len(batch_ids)
        ):
            raise ValueError("duplicate model result")

        self.model_calls.extend(batch)
        self.messages[assistant_snapshot.id] = assistant_snapshot
        self.saved_model_results.append((batch, assistant_snapshot))


class LegacyModelStore:
    """Minimal pre-orchestration store without the batch result API."""

    def __init__(self) -> None:
        self.messages: dict[str, StoredMessage] = {}
        self.model_calls: list[ModelCallRecord] = []
        self.saved_results: list[tuple[ModelCallRecord, StoredMessage]] = []

    async def claim_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        return True

    async def claim_message(self, item: StoredMessage) -> bool:
        if item.id in self.messages:
            return False
        self.messages[item.id] = item
        return True

    async def find_message(
        self,
        message_id: str,
    ) -> StoredMessage | None:
        return self.messages.get(message_id)

    async def find_assistant_by_correlation(
        self,
        correlation_id: str,
    ) -> StoredMessage | None:
        return next(
            (
                item
                for item in self.messages.values()
                if item.role == "assistant"
                and item.correlation_id == correlation_id
            ),
            None,
        )

    async def recent_messages(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]:
        return [
            item
            for item in self.messages.values()
            if item.conversation_id == conversation_id
        ][-limit:]

    async def list_memories(
        self,
        source: str,
        owner_id: str,
    ) -> list[MemoryItem]:
        return []

    async def record_model_call(self, record: ModelCallRecord) -> None:
        self.model_calls.append(record)

    async def save_model_result(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None:
        self.model_calls.append(record)
        self.messages[assistant_message.id] = assistant_message
        self.saved_results.append((record, assistant_message))


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
        self.llm = FakeLanguageModel()
        self.store = FakeStore()
        self.context_builder = ConversationContextBuilder(4, 1000)
        self.application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=self.store,
            context_builder=self.context_builder,
            publisher=self.publisher,
        )

    def test_process_runs_business_logic_without_publishing(self):
        result = asyncio.run(self.application.process(message()))

        self.assertEqual(result.text, "模型回答")
        self.assertIsNone(self.application.model_orchestrator)
        self.assertEqual(len(self.llm.requests), 1)
        self.assertIn("message-1", self.store.messages)
        self.subscriber.assert_not_awaited()

    def test_uninjected_application_supports_legacy_single_result_store(self):
        store = LegacyModelStore()
        application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=store,
            context_builder=self.context_builder,
        )

        result = asyncio.run(
            application.process(message(message_id="legacy-store"))
        )

        self.assertEqual(result.text, "模型回答")
        self.assertEqual(len(self.llm.requests), 1)
        self.assertEqual(len(store.saved_results), 1)
        record, assistant = store.saved_results[0]
        self.assertEqual(record.message_id, "legacy-store")
        self.assertEqual(assistant.correlation_id, "legacy-store")

    def test_uninjected_application_records_legacy_model_failure_once(self):
        error = ModelTimeoutError("internal timeout details")
        llm = FakeLanguageModel(error=error)
        store = LegacyModelStore()
        application = AssistantApplication(
            tts=self.tts,
            llm=llm,
            store=store,
            context_builder=self.context_builder,
        )

        result = asyncio.run(
            application.process(message(message_id="legacy-error"))
        )

        self.assertEqual(result.kind, ResponseKind.ERROR)
        self.assertEqual(result.text, "模型响应超时，请稍后再试。")
        self.assertEqual(len(store.model_calls), 1)
        self.assertEqual(store.model_calls[0].status, error.code)
        self.assertEqual(list(store.messages), ["legacy-error"])

    def test_handle_processes_then_publishes_once(self):
        result = asyncio.run(self.application.handle(message()))

        self.assertEqual(result.text, "模型回答")
        self.subscriber.assert_awaited_once_with(result)

    def test_has_seen_message_uses_the_store_without_publishing(self):
        item = message(
            conversation_id="qq:private:456",
            message_id="qq:123:10",
            source=MessageSource.QQ,
            sender_id="456",
        )

        self.assertFalse(
            asyncio.run(
                self.application.has_seen_message(item.message_id)
            )
        )
        asyncio.run(self.application.process(item))

        self.assertTrue(
            asyncio.run(
                self.application.has_seen_message(item.message_id)
            )
        )
        self.subscriber.assert_not_awaited()

    def test_fake_store_satisfies_minimal_application_protocol(self):
        self.assertIsInstance(self.store, AssistantStore)

    def test_fake_store_saves_model_results_in_stable_batch_order(self):
        assistant = StoredMessage(
            id="assistant-batch",
            conversation_id="desktop:user-1",
            correlation_id="message-1",
            role="assistant",
            content="final response",
        )
        records = [
            ModelCallRecord(
                id="call-1",
                message_id="message-1",
                model="fake-model",
                status="succeeded",
                latency_ms=10,
            ),
            ModelCallRecord(
                id="call-2",
                message_id="message-1",
                model="fake-model",
                status="succeeded",
                latency_ms=20,
            ),
        ]

        asyncio.run(self.store.save_model_results(records, assistant))

        self.assertEqual(self.store.model_calls, records)
        self.assertEqual(
            self.store.saved_model_results,
            [(records, assistant)],
        )
        self.assertEqual(self.store.messages[assistant.id], assistant)
        self.assertIsNot(self.store.messages[assistant.id], assistant)

    def test_fake_store_snapshots_batch_values_before_returning(self):
        assistant = StoredMessage(
            id="assistant-snapshot",
            conversation_id="desktop:user-1",
            correlation_id="message-1",
            role="assistant",
            content="original response",
        )
        record = ModelCallRecord(
            id="call-snapshot",
            message_id="message-1",
            model="original-model",
            status="succeeded",
            latency_ms=10,
        )

        asyncio.run(self.store.save_model_results([record], assistant))
        record.model = "mutated-model"
        record.status = "mutated-status"
        assistant.content = "mutated response"

        stored_records, stored_assistant = self.store.saved_model_results[0]
        self.assertEqual(stored_records[0].model, "original-model")
        self.assertEqual(stored_records[0].status, "succeeded")
        self.assertEqual(stored_assistant.content, "original response")
        self.assertIsNot(stored_records[0], record)
        self.assertIsNot(stored_assistant, assistant)
        self.assertEqual(
            self.store.messages[assistant.id].content,
            "original response",
        )

    def test_fake_store_single_result_wrapper_snapshots_values(self):
        assistant = StoredMessage(
            id="assistant-single-snapshot",
            conversation_id="desktop:user-1",
            correlation_id="message-1",
            role="assistant",
            content="original response",
        )
        record = ModelCallRecord(
            id="call-single-snapshot",
            message_id="message-1",
            model="original-model",
            status="succeeded",
            latency_ms=10,
        )

        asyncio.run(self.store.save_model_result(record, assistant))
        record.model = "mutated-model"
        assistant.content = "mutated response"

        stored_records, stored_assistant = self.store.saved_model_results[0]
        self.assertEqual(stored_records[0].model, "original-model")
        self.assertEqual(stored_assistant.content, "original response")
        self.assertIsNot(stored_records[0], record)
        self.assertIsNot(stored_assistant, assistant)

    def test_fake_store_rejects_invalid_batch_without_partial_state(self):
        existing = ModelCallRecord(
            id="duplicate-call",
            message_id="message-1",
            model="fake-model",
            status="succeeded",
            latency_ms=5,
        )
        self.store.model_calls.append(existing)
        assistant = StoredMessage(
            id="assistant-batch",
            conversation_id="desktop:user-1",
            correlation_id="message-1",
            role="assistant",
            content="must not be saved",
        )
        records = [
            ModelCallRecord(
                id="new-call",
                message_id="message-1",
                model="fake-model",
                status="succeeded",
                latency_ms=10,
            ),
            existing,
        ]

        with self.assertRaisesRegex(ValueError, "duplicate model result"):
            asyncio.run(self.store.save_model_results(records, assistant))

        self.assertEqual(self.store.model_calls, [existing])
        self.assertNotIn(assistant.id, self.store.messages)
        self.assertEqual(self.store.saved_model_results, [])

    def test_chat_persists_messages_model_metadata_context_and_publishes(self):
        previous_user = StoredMessage(
            id="previous-user",
            conversation_id="desktop:user-1",
            role="user",
            content="上一句",
        )
        self.store.messages[previous_user.id] = previous_user
        scoped_memory = MemoryItem(
            source="desktop",
            owner_id="user-1",
            content="喜欢红茶",
            normalized_content="喜欢红茶",
        )
        other_memory = MemoryItem(
            source="desktop",
            owner_id="other-user",
            content="不应出现",
            normalized_content="不应出现",
        )
        self.store.memories[
            ("desktop", "user-1", scoped_memory.normalized_content)
        ] = scoped_memory
        self.store.memories[
            ("desktop", "other-user", other_memory.normalized_content)
        ] = other_memory
        item = message()

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.kind, ResponseKind.SPEAK)
        self.assertEqual(result.text, "模型回答")
        user = self.store.messages[item.message_id]
        self.assertEqual(user.role, "user")
        self.assertEqual(user.content, "你好")
        self.assertEqual(user.created_at, item.timestamp)
        self.assertEqual(
            self.store.conversations[item.conversation_id],
            ("desktop", "user-1"),
        )
        self.assertEqual(len(self.llm.requests), 1)
        request = self.llm.requests[0]
        self.assertEqual(request.correlation_id, item.message_id)
        request_text = "\n".join(part.content for part in request.messages)
        self.assertIn("上一句", request_text)
        self.assertIn("喜欢红茶", request_text)
        self.assertNotIn("不应出现", request_text)
        records, assistant = self.store.saved_model_results[0]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.message_id, item.message_id)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.model, "fake-model-v2")
        self.assertEqual(record.prompt_tokens, 12)
        self.assertEqual(record.completion_tokens, 5)
        self.assertEqual(record.provider_request_id, "provider-request-1")
        self.assertGreaterEqual(record.latency_ms, 0)
        self.assertEqual(assistant.id, result.response_id)
        self.assertEqual(assistant.correlation_id, item.message_id)
        self.assertEqual(assistant.model, "fake-model-v2")
        self.assertEqual(assistant.status, MessageStatus.COMPLETED)
        self.subscriber.assert_awaited_once_with(result)

    def test_recent_messages_uses_public_builder_limit(self):
        asyncio.run(self.application.handle(message()))

        self.assertEqual(self.store.recent_limits, [4])

    def test_remember_and_forget_are_scoped_and_persist_local_responses(self):
        remember = message(
            message_id="remember-1",
            text="记住： 喜欢红茶 ",
        )
        remembered = asyncio.run(self.application.handle(remember))

        self.assertEqual(remembered.text, "已经记住了。")
        self.assertEqual(len(self.llm.requests), 0)
        saved = self.store.memories[("desktop", "user-1", "喜欢红茶")]
        self.assertEqual(saved.source_message_id, "remember-1")
        remember_reply = self.store.messages[remembered.response_id]
        self.assertEqual(remember_reply.model, "local-memory")
        self.assertEqual(remember_reply.status, MessageStatus.COMPLETED)

        self.store.memories[
            ("desktop", "other-user", "喜欢红茶")
        ] = MemoryItem(
            source="desktop",
            owner_id="other-user",
            content="喜欢红茶",
            normalized_content="喜欢红茶",
        )
        self.store.memories[
            ("qq", "user-1", "喜欢红茶")
        ] = MemoryItem(
            source="qq",
            owner_id="user-1",
            content="喜欢红茶",
            normalized_content="喜欢红茶",
        )
        forget = message(
            message_id="forget-1",
            text="忘记：喜欢红茶",
        )
        forgotten = asyncio.run(self.application.handle(forget))

        self.assertEqual(forgotten.text, "已经忘记了。")
        self.assertNotIn(("desktop", "user-1", "喜欢红茶"), self.store.memories)
        self.assertIn(("desktop", "other-user", "喜欢红茶"), self.store.memories)
        self.assertIn(("qq", "user-1", "喜欢红茶"), self.store.memories)
        self.assertEqual(len(self.llm.requests), 0)
        forget_reply = self.store.messages[forgotten.response_id]
        self.assertEqual(forget_reply.model, "local-memory")

    def test_forget_missing_memory_returns_persisted_local_reply(self):
        result = asyncio.run(
            self.application.handle(
                message(message_id="forget-missing", text="忘记：不存在"),
            )
        )

        self.assertEqual(result.kind, ResponseKind.SPEAK)
        self.assertEqual(result.text, "没有找到完全匹配的记忆。")
        stored = self.store.messages[result.response_id]
        self.assertEqual(stored.model, "local-memory")
        self.assertEqual(stored.status, MessageStatus.COMPLETED)
        self.assertEqual(len(self.llm.requests), 0)

    def test_empty_memory_command_returns_persisted_failed_error(self):
        result = asyncio.run(
            self.application.handle(
                message(message_id="empty-memory", text="记住："),
            )
        )

        self.assertEqual(result.kind, ResponseKind.ERROR)
        self.assertIn("不能为空", result.text)
        stored = self.store.messages[result.response_id]
        self.assertEqual(stored.model, "local-memory")
        self.assertEqual(stored.status, MessageStatus.FAILED)
        self.assertEqual(len(self.llm.requests), 0)

    def test_duplicate_remember_reuses_response_without_duplicate_memory(self):
        item = message(message_id="remember-once", text="记住：红茶")

        first = asyncio.run(self.application.handle(item))
        second = asyncio.run(self.application.handle(item))

        self.assertEqual(second.response_id, first.response_id)
        self.assertEqual(second.text, first.text)
        self.assertEqual(len(self.store.memories), 1)
        self.assertEqual(len(self.llm.requests), 0)

    def test_duplicate_successful_chat_calls_model_once_and_reuses_response(self):
        item = message(message_id="chat-once")

        first = asyncio.run(self.application.handle(item))
        second = asyncio.run(self.application.handle(item))

        self.assertEqual(len(self.llm.requests), 1)
        self.assertEqual(second.response_id, first.response_id)
        self.assertEqual(second.text, first.text)
        self.assertEqual(second.kind, ResponseKind.SPEAK)

    def test_chat_uses_orchestrator_and_saves_all_model_attempts(self):
        orchestrator = Mock()
        orchestrator.run = AsyncMock(
            return_value=ModelOrchestrationResult(
                reply=ModelReply(text="现在是 12:00", model="fake"),
                attempts=[
                    ModelAttempt(
                        model="fake",
                        status="succeeded",
                        latency_ms=5,
                        prompt_tokens=10,
                    ),
                    ModelAttempt(
                        model="fake",
                        status="succeeded",
                        latency_ms=4,
                        completion_tokens=3,
                    ),
                ],
            )
        )
        store = FakeStore()
        application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=store,
            context_builder=self.context_builder,
            model_orchestrator=orchestrator,
        )
        item = message(message_id="message-tools")

        first = asyncio.run(application.process(item))
        second = asyncio.run(application.process(item))

        self.assertEqual(first.text, "现在是 12:00")
        self.assertEqual(second.response_id, first.response_id)
        self.assertEqual(second.text, first.text)
        orchestrator.run.assert_awaited_once()
        self.assertEqual(len(store.model_calls), 2)
        self.assertEqual(
            [record.prompt_tokens for record in store.model_calls],
            [10, None],
        )
        self.assertEqual(
            [record.completion_tokens for record in store.model_calls],
            [None, 3],
        )
        self.assertEqual(len(store.saved_model_results), 1)
        self.assertEqual(self.llm.requests, [])

    def test_orchestration_failure_saves_attempts_and_hides_internal_error(self):
        secret = "secret-api-key-and-provider-body"
        public_error = ModelProtocolError(secret)
        error = ModelToolOrchestrationError(
            error=public_error,
            attempts=[
                ModelAttempt(
                    model="fake",
                    status="succeeded",
                    latency_ms=5,
                    prompt_tokens=10,
                ),
                ModelAttempt(
                    model="fake",
                    status=public_error.code,
                    latency_ms=4,
                ),
            ],
        )
        orchestrator = Mock()
        orchestrator.run = AsyncMock(side_effect=error)
        store = FakeStore()
        store.record_model_call = AsyncMock(
            side_effect=AssertionError("must use atomic batch persistence")
        )
        application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=store,
            context_builder=self.context_builder,
            model_orchestrator=orchestrator,
        )
        item = message(message_id="message-tools-error")

        first = asyncio.run(application.process(item))
        second = asyncio.run(application.process(item))

        self.assertEqual(first.kind, ResponseKind.ERROR)
        self.assertEqual(first.text, "模型服务返回了无法处理的响应。")
        self.assertNotIn(secret, first.text)
        self.assertEqual(second.response_id, first.response_id)
        self.assertEqual(second.kind, ResponseKind.ERROR)
        self.assertEqual(second.text, first.text)
        orchestrator.run.assert_awaited_once()
        store.record_model_call.assert_not_awaited()
        self.assertEqual(
            [record.status for record in store.model_calls],
            ["succeeded", ModelProtocolError.code],
        )
        self.assertEqual(
            [record.message_id for record in store.model_calls],
            [item.message_id, item.message_id],
        )
        self.assertEqual(len(store.saved_model_results), 1)
        records, assistant = store.saved_model_results[0]
        self.assertEqual(records, store.model_calls)
        self.assertEqual(assistant.id, first.response_id)
        self.assertEqual(assistant.correlation_id, item.message_id)
        self.assertEqual(assistant.content, first.text)
        self.assertEqual(assistant.model, "fake")
        self.assertEqual(assistant.status, MessageStatus.FAILED)
        self.assertEqual(
            set(store.messages),
            {item.message_id, first.response_id},
        )

    def test_orchestration_failure_batch_error_has_no_partial_state(self):
        public_error = ModelProtocolError("internal details")
        error = ModelToolOrchestrationError(
            error=public_error,
            attempts=[
                ModelAttempt(
                    model="fake",
                    status=public_error.code,
                    latency_ms=4,
                ),
            ],
        )
        orchestrator = Mock()
        orchestrator.run = AsyncMock(side_effect=error)
        store = FakeStore()
        store.record_model_call = AsyncMock(
            side_effect=AssertionError("must not record attempts separately")
        )
        store.save_model_results = AsyncMock(
            side_effect=RuntimeError("injected transaction failure")
        )
        application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=store,
            context_builder=self.context_builder,
            model_orchestrator=orchestrator,
        )
        item = message(message_id="message-tools-save-error")

        with self.assertRaisesRegex(
            RuntimeError,
            "injected transaction failure",
        ):
            asyncio.run(application.process(item))

        store.save_model_results.assert_awaited_once()
        store.record_model_call.assert_not_awaited()
        self.assertEqual(store.model_calls, [])
        self.assertEqual(list(store.messages), [item.message_id])
        self.assertEqual(store.saved_model_results, [])

    def test_orchestration_failure_without_attempts_is_not_replayed(self):
        error = ModelToolLimitError("model_tool_round_limit", attempts=[])
        orchestrator = Mock()
        orchestrator.run = AsyncMock(side_effect=error)
        store = FakeStore()
        store.record_model_call = AsyncMock()
        store.save_model_results = AsyncMock()
        application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=store,
            context_builder=self.context_builder,
            model_orchestrator=orchestrator,
        )
        item = message(message_id="message-tools-no-attempts")

        first = asyncio.run(application.process(item))
        second = asyncio.run(application.process(item))

        self.assertEqual(first.kind, ResponseKind.ERROR)
        self.assertEqual(first.text, "模型服务暂时不可用，请稍后再试。")
        self.assertEqual(second.kind, ResponseKind.ERROR)
        self.assertNotEqual(second.response_id, first.response_id)
        self.assertNotEqual(second.text, first.text)
        orchestrator.run.assert_awaited_once()
        store.record_model_call.assert_not_awaited()
        store.save_model_results.assert_not_awaited()
        self.assertEqual(list(store.messages), [item.message_id])

    def test_duplicate_user_without_assistant_returns_generic_error(self):
        item = message(message_id="orphan-user")
        self.store.messages[item.message_id] = StoredMessage(
            id=item.message_id,
            conversation_id=item.conversation_id,
            role="user",
            content=item.content.text,
        )

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.kind, ResponseKind.ERROR)
        self.assertNotIn(item.content.text, result.text)
        self.assertEqual(len(self.llm.requests), 0)

    def test_cross_conversation_message_id_does_not_leak_assistant_text(self):
        item = message(
            conversation_id="desktop:new-user",
            message_id="shared-id",
            sender_id="new-user",
        )
        self.store.messages[item.message_id] = StoredMessage(
            id=item.message_id,
            conversation_id="desktop:old-user",
            role="user",
            content="旧问题",
        )
        self.store.messages["secret-assistant"] = StoredMessage(
            id="secret-assistant",
            conversation_id="desktop:old-user",
            correlation_id=item.message_id,
            role="assistant",
            content="其他会话的秘密回答",
        )

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.kind, ResponseKind.ERROR)
        self.assertNotIn("秘密回答", result.text)
        self.assertEqual(len(self.llm.requests), 0)

    def test_existing_failed_local_response_is_reused_as_error(self):
        item = message(message_id="failed-local")
        self.store.messages[item.message_id] = StoredMessage(
            id=item.message_id,
            conversation_id=item.conversation_id,
            role="user",
            content=item.content.text,
        )
        self.store.messages["failed-response"] = StoredMessage(
            id="failed-response",
            conversation_id=item.conversation_id,
            correlation_id=item.message_id,
            role="assistant",
            content="记忆内容不能为空。",
            model="local-memory",
            status=MessageStatus.FAILED,
        )

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.response_id, "failed-response")
        self.assertEqual(result.kind, ResponseKind.ERROR)
        self.assertEqual(result.text, "记忆内容不能为空。")

    def test_model_errors_are_safely_mapped_and_only_user_is_saved(self):
        cases = (
            (ModelAuthenticationError, "认证"),
            (ModelRateLimitError, "频繁"),
            (ModelTimeoutError, "超时"),
            (ModelProtocolError, "响应"),
            (ModelGatewayError, "暂时不可用"),
        )

        for index, (error_type, expected_text) in enumerate(cases):
            with self.subTest(error_type=error_type):
                secret = "secret-api-key-and-provider-body"
                llm = FakeLanguageModel(error=error_type(secret))
                store = FakeStore()
                application = AssistantApplication(
                    tts=self.tts,
                    llm=llm,
                    store=store,
                    context_builder=self.context_builder,
                )
                item = message(message_id=f"error-{index}")

                result = asyncio.run(application.handle(item))

                self.assertEqual(result.kind, ResponseKind.ERROR)
                self.assertIn(expected_text, result.text)
                self.assertNotIn(secret, result.text)
                self.assertEqual(list(store.messages), [item.message_id])
                self.assertEqual(len(store.model_calls), 1)
                self.assertEqual(store.model_calls[0].status, error_type.code)
                self.assertEqual(store.model_calls[0].model, llm.model_name)
                self.assertEqual(store.model_calls[0].message_id, item.message_id)

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


class AssistantApplicationSqliteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "assistant.db"
        )
        self.store = SqliteStore(self.database_path)
        self.tts = AsyncMock()
        self.llm = FakeLanguageModel()
        self.publisher = ResponsePublisher()
        self.subscriber = AsyncMock()
        self.publisher.subscribe(self.subscriber)
        self.application = AssistantApplication(
            tts=self.tts,
            llm=self.llm,
            store=self.store,
            context_builder=ConversationContextBuilder(4, 1000),
            publisher=self.publisher,
        )

    def tearDown(self):
        asyncio.run(self.store.close())
        self.temporary_directory.cleanup()

    def test_conversation_scope_cannot_be_reassigned_to_another_owner(self):
        alice = message(
            conversation_id="shared-conversation",
            message_id="alice-message",
            sender_id="alice",
        )
        bob = message(
            conversation_id="shared-conversation",
            message_id="bob-message",
            sender_id="bob",
        )

        alice_result = asyncio.run(self.application.handle(alice))
        bob_result = asyncio.run(self.application.handle(bob))

        self.assertEqual(alice_result.kind, ResponseKind.SPEAK)
        self.assertEqual(bob_result.kind, ResponseKind.ERROR)
        self.assertEqual(len(self.llm.requests), 1)
        with sqlite3.connect(self.database_path) as connection:
            owner = connection.execute(
                "SELECT source, owner_id FROM conversations WHERE id = ?",
                ("shared-conversation",),
            ).fetchone()
            bob_message = connection.execute(
                "SELECT 1 FROM messages WHERE id = ?",
                ("bob-message",),
            ).fetchone()
        self.assertEqual(owner, ("desktop", "alice"))
        self.assertIsNone(bob_message)
        self.subscriber.assert_has_awaits(
            [
                unittest.mock.call(alice_result),
                unittest.mock.call(bob_result),
            ]
        )

    def test_same_message_id_with_changed_content_does_not_replay(self):
        original = message(message_id="same-id", text="原问题")
        changed = message(message_id="same-id", text="修改后的问题")

        original_result = asyncio.run(self.application.handle(original))
        changed_result = asyncio.run(self.application.handle(changed))

        self.assertEqual(original_result.kind, ResponseKind.SPEAK)
        self.assertEqual(changed_result.kind, ResponseKind.ERROR)
        self.assertNotEqual(changed_result.text, original_result.text)
        self.assertNotIn(original_result.text, changed_result.text)
        self.assertEqual(len(self.llm.requests), 1)

    def test_concurrent_cross_conversation_message_id_is_safe_and_published(self):
        first = message(
            conversation_id="conversation-a",
            message_id="shared-message-id",
            sender_id="alice",
        )
        second = message(
            conversation_id="conversation-b",
            message_id="shared-message-id",
            sender_id="bob",
        )

        async def run_both():
            return await asyncio.gather(
                self.application.handle(first),
                self.application.handle(second),
                return_exceptions=False,
            )

        results = asyncio.run(run_both())

        self.assertCountEqual(
            [result.kind for result in results],
            [ResponseKind.SPEAK, ResponseKind.ERROR],
        )
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT conversation_id, role FROM messages WHERE id = ?",
                ("shared-message-id",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "user")
        self.assertEqual(self.subscriber.await_count, 2)
