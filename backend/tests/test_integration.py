import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app
from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.model_tools import ModelToolOrchestrator
from channels.desktop import LOCAL_USER
from core.runtime import AssistantRuntime
from infrastructure.sqlite_store import SqliteStore
from llm.errors import ModelServiceError
from llm.models import ModelReply, ModelRequest, ModelToolCall
from memory.models import MemoryItem, StoredMessage
from domain.tools import ToolRisk
from tools.catalog import ModelToolCatalog
from tools.registry import ToolDefinition


class QueuedFakeGateway:
    model_name = "fake-model"

    def __init__(self) -> None:
        self.replies = [
            ModelReply(
                text="主人说得有道理~",
                model=self.model_name,
            )
        ]
        self.requests: list[ModelRequest] = []
        self.error: Exception | None = None
        self.complete = AsyncMock(side_effect=self._complete)

    async def _complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise AssertionError("fake model reply queue exhausted")
        return self.replies.pop(0)


class HighRiskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=100)


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SqliteStore(Path(self.directory.name) / "assistant.db")
        self.llm = QueuedFakeGateway()
        tts = Mock()
        tts.synthesize = AsyncMock(return_value=None)
        application = AssistantApplication(
            tts=tts,
            llm=self.llm,
            store=self.store,
            context_builder=ConversationContextBuilder(20, 12000),
            publisher=ResponsePublisher(),
        )
        monitor = Mock()
        monitor.get_status.return_value = {
            "cpu": {"percent": 1},
            "memory": {"percent": 2},
        }
        scenario_engine = Mock()
        scenario_engine.detect.return_value = None
        self.runtime = AssistantRuntime(
            monitor=monitor,
            application=application,
            scenario_engine=scenario_engine,
        )
        catalog = ModelToolCatalog(self.runtime.tool_registry)
        orchestrator = ModelToolOrchestrator(
            gateway=self.llm,
            catalog=catalog,
            tool_service=self.runtime.tool_service,
            enabled=False,
        )
        application.model_orchestrator = orchestrator
        self.runtime.model_tool_catalog = catalog
        self.runtime.model_tool_orchestrator = orchestrator
        self.app = create_app(runtime_instance=self.runtime)

    def register_high_risk_tool(self):
        calls: list[str] = []

        async def handler(arguments: HighRiskArguments) -> dict:
            calls.append(arguments.target)
            return {"target": arguments.target}

        self.runtime.tool_registry.register(
            ToolDefinition(
                name="computer.example",
                title="示例电脑操作",
                arguments_model=HighRiskArguments,
                risk=ToolRisk.HIGH,
                impact="将修改本机示例目标",
                timeout_seconds=2,
                cancellable=True,
                handler=handler,
            )
        )
        return calls

    def test_low_risk_tool_completes_without_confirmation(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/api/tools/requests",
                json={
                    "tool": "system.current_time",
                    "arguments": {"timezone": "UTC"},
                    "correlationId": "manual-1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "succeeded")
        self.assertEqual(response.json()["result"]["timezone"], "UTC")
        self.assertIsNone(response.json()["confirmation"])

    def test_high_risk_tool_broadcasts_lists_and_rejects_without_execution(self):
        calls = self.register_high_risk_tool()
        payload = {
            "tool": "computer.example",
            "arguments": {"target": "example"},
            "correlationId": "manual-high-1",
        }

        with TestClient(self.app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                response = client.post("/api/tools/requests", json=payload)
                event = websocket.receive_json()
            listed = client.get("/api/tools/confirmations")
            confirmation_id = response.json()["confirmation"]["id"]
            rejected = client.post(
                f"/api/tools/confirmations/{confirmation_id}/decision",
                json={"decision": "reject"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            event["type"],
            "tool_confirmation_required",
        )
        self.assertEqual(
            event["confirmation"]["requestId"],
            response.json()["requestId"],
        )
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["state"], "rejected")
        self.assertEqual(calls, [])

    def test_high_risk_approval_executes_once_and_can_be_queried(self):
        calls = self.register_high_risk_tool()

        with TestClient(self.app) as client:
            pending = client.post(
                "/api/tools/requests",
                json={
                    "tool": "computer.example",
                    "arguments": {"target": "example"},
                    "correlationId": "manual-high-2",
                },
            )
            confirmation_id = pending.json()["confirmation"]["id"]
            approved = client.post(
                f"/api/tools/confirmations/{confirmation_id}/decision",
                json={"decision": "approve"},
            )
            replay = client.post(
                f"/api/tools/confirmations/{confirmation_id}/decision",
                json={"decision": "approve"},
            )
            queried = client.get(
                f"/api/tools/requests/{pending.json()['requestId']}"
            )

        self.assertEqual(approved.json()["state"], "succeeded")
        self.assertEqual(replay.json()["state"], "succeeded")
        self.assertEqual(queried.json()["state"], "succeeded")
        self.assertEqual(calls, ["example"])

    def test_tool_errors_and_cancel_are_safe(self):
        calls = self.register_high_risk_tool()

        with TestClient(self.app) as client:
            unknown = client.post(
                "/api/tools/requests",
                json={
                    "tool": "private.unknown",
                    "arguments": {"api_key": "secret"},
                },
            )
            invalid = client.post(
                "/api/tools/requests",
                json={
                    "tool": "computer.example",
                    "arguments": {"target": ""},
                },
            )
            pending = client.post(
                "/api/tools/requests",
                json={
                    "tool": "computer.example",
                    "arguments": {"target": "cancel-me"},
                },
            )
            cancelled = client.post(
                f"/api/tools/requests/{pending.json()['requestId']}/cancel"
            )
            missing = client.get("/api/tools/requests/missing")

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json(), {"detail": "tool not found"})
        self.assertNotIn("secret", unknown.text)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json(),
            {"detail": "tool arguments are invalid"},
        )
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(calls, [])

    def tearDown(self):
        asyncio.run(self.runtime.aclose())
        asyncio.run(self.store.close())
        self.directory.cleanup()
        from api import ws

        ws._sessions.clear()

    def test_status_endpoint_returns_metrics_and_safe_llm_mode(self):
        with TestClient(self.app) as client:
            response = client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("cpu", response.json())
        self.assertIn("memory", response.json())
        self.assertEqual(response.json()["assistant"], {"llmMode": "demo"})

    def test_websocket_interaction_is_dispatched_and_broadcast(self):
        with TestClient(self.app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                websocket.send_json(
                    {
                        "type": "interaction",
                        "action": "click",
                        "messageId": "interaction-id-must-be-ignored",
                    }
                )
                response = websocket.receive_json()

        self.assertEqual(response["type"], "action")
        self.assertEqual(response["motion"], "tap_body")
        self.assertNotEqual(
            response["correlationId"],
            "interaction-id-must-be-ignored",
        )

    def test_invalid_websocket_payload_returns_error(self):
        with TestClient(self.app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                websocket.send_text("[]")
                response = websocket.receive_json()

        self.assertEqual(response["type"], "error")
        self.assertIn("JSON object", response["message"])

    def test_websocket_oversized_message_id_returns_safe_error(self):
        oversized = "private-" + ("x" * 193)

        with TestClient(self.app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                websocket.send_json(
                    {
                        "type": "chat",
                        "content": "你好",
                        "messageId": oversized,
                    }
                )
                response = websocket.receive_json()

        self.assertEqual(
            response,
            {
                "type": "error",
                "message": "messageId must be between 1 and 200 characters",
            },
        )
        self.assertNotIn(oversized, str(response))

    def test_chat_http_contract_and_message_id_idempotency(self):
        payload = {
            "source": "desktop",
            "senderId": LOCAL_USER.id,
            "content": "你好",
            "messageId": "stable-http-message",
        }

        with TestClient(self.app) as client:
            first = client.post("/api/chat/message", json=payload)
            replay = client.post("/api/chat/message", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            first.json(),
            {"reply": "主人说得有道理~", "status": "ok"},
        )
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.llm.complete.await_count, 1)

    def test_desktop_chat_uses_model_time_tool_without_confirmation(self):
        self.llm.replies = [
            ModelReply(
                text=None,
                tool_calls=[
                    ModelToolCall(
                        id="time-call",
                        name="system.current_time",
                        arguments={"timezone": "UTC"},
                    )
                ],
                model="fake-model",
                prompt_tokens=10,
                completion_tokens=2,
                provider_request_id="desktop-tool-1",
            ),
            ModelReply(
                text="已读取 UTC 时间",
                model="fake-model",
                prompt_tokens=20,
                completion_tokens=4,
                provider_request_id="desktop-tool-2",
            ),
        ]
        self.runtime.model_tool_orchestrator.enabled = True

        with TestClient(self.app) as client:
            response = client.post(
                "/api/chat/message",
                json={
                    "source": "desktop",
                    "senderId": LOCAL_USER.id,
                    "content": "UTC 现在几点？",
                    "messageId": "desktop-tool-message",
                },
            )
            confirmations = client.get("/api/tools/confirmations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "已读取 UTC 时间", "status": "ok"},
        )
        self.assertEqual(confirmations.json(), [])
        self.assertEqual(self.llm.complete.await_count, 2)
        self.assertEqual(len(self.llm.requests), 2)
        self.assertEqual(
            [tool.name for tool in self.llm.requests[0].tools],
            ["system.current_time"],
        )
        self.assertEqual(
            self.llm.requests[1].messages[-1].role.value,
            "tool",
        )
        self.assertIs(
            self.runtime.tool_registry.require(
                "system.current_time"
            ).risk,
            ToolRisk.LOW,
        )
        with closing(
            sqlite3.connect(self.store.database_path)
        ) as connection:
            source, state, request_id = connection.execute(
                "SELECT source, state, id FROM tool_requests "
                "WHERE tool_name = 'system.current_time'"
            ).fetchone()
            audit_events = connection.execute(
                "SELECT event_type FROM tool_audit_events "
                "WHERE request_id = ? ORDER BY created_at",
                (request_id,),
            ).fetchall()
            model_calls = connection.execute(
                "SELECT status, provider_request_id FROM model_calls "
                "WHERE message_id = ? ORDER BY created_at, id",
                ("desktop-tool-message",),
            ).fetchall()
            assistant = connection.execute(
                "SELECT content, status FROM messages "
                "WHERE correlation_id = ? AND role = 'assistant'",
                ("desktop-tool-message",),
            ).fetchone()
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM tool_confirmations "
                "WHERE state = 'pending'"
            ).fetchone()[0]

        self.assertEqual((source, state), ("model", "succeeded"))
        self.assertEqual(
            [event[0] for event in audit_events],
            ["requested", "execution_started", "succeeded"],
        )
        self.assertEqual(
            model_calls,
            [
                ("succeeded", "desktop-tool-1"),
                ("succeeded", "desktop-tool-2"),
            ],
        )
        self.assertEqual(assistant, ("已读取 UTC 时间", "completed"))
        self.assertEqual(pending_count, 0)

    def test_chat_model_error_is_a_safe_503(self):
        self.llm.error = ModelServiceError(
            "private provider body with api-key"
        )

        with TestClient(self.app) as client:
            response = client.post(
                "/api/chat/message",
                json={
                    "source": "desktop",
                    "senderId": LOCAL_USER.id,
                    "content": "你好",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "模型服务暂时不可用，请稍后再试。"},
        )
        self.assertNotIn("api-key", response.text)

    def test_memory_crud_normalizes_upserts_and_enforces_local_scope(self):
        other = asyncio.run(
            self.store.save_memory(
                MemoryItem(
                    id="other-memory",
                    source="qq",
                    owner_id="qq-user",
                    content="私密数据",
                    normalized_content="私密数据",
                )
            )
        )

        with TestClient(self.app) as client:
            created = client.post(
                "/api/memories",
                json={"content": "  喜欢   红茶  "},
            )
            updated = client.post(
                "/api/memories",
                json={"content": "喜欢 红茶"},
            )
            listed = client.get("/api/memories")
            forbidden = client.delete(f"/api/memories/{other.id}")
            deleted = client.delete(
                f"/api/memories/{created.json()['id']}"
            )
            missing = client.delete(
                f"/api/memories/{created.json()['id']}"
            )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["content"], "喜欢   红茶")
        self.assertEqual(updated.status_code, 201)
        self.assertEqual(updated.json()["id"], created.json()["id"])
        self.assertEqual(
            [item["content"] for item in listed.json()],
            ["喜欢 红茶"],
        )
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(missing.status_code, 404)

    def test_memory_create_rejects_blank_and_oversized_content(self):
        with TestClient(self.app) as client:
            blank = client.post("/api/memories", json={"content": " \n "})
            oversized = client.post(
                "/api/memories",
                json={"content": "x" * 2001},
            )

        self.assertEqual(blank.status_code, 422)
        self.assertEqual(oversized.status_code, 422)

    def test_local_conversation_messages_can_be_read_and_deleted(self):
        with TestClient(self.app) as client:
            chat = client.post(
                "/api/chat/message",
                json={
                    "source": "desktop",
                    "senderId": LOCAL_USER.id,
                    "content": "你好",
                    "messageId": "conversation-message-1",
                },
            )
            listed = client.get(
                f"/api/conversations/desktop:{LOCAL_USER.id}/messages"
            )
            deleted = client.delete(
                f"/api/conversations/desktop:{LOCAL_USER.id}"
            )
            empty = client.get(
                f"/api/conversations/desktop:{LOCAL_USER.id}/messages"
            )
            missing = client.delete(
                f"/api/conversations/desktop:{LOCAL_USER.id}"
            )

        self.assertEqual(chat.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [message["role"] for message in listed.json()],
            ["user", "assistant"],
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(empty.json(), [])
        self.assertEqual(missing.status_code, 404)

    def test_conversation_api_hides_all_non_local_conversations(self):
        conversations = (
            ("qq:secret", "qq", "qq-user"),
            ("scenario:high_cpu", "scenario", "scenario-engine"),
            ("desktop:other-user", "desktop", "other-user"),
        )
        for index, (conversation_id, source, owner_id) in enumerate(conversations):
            asyncio.run(
                self.store.upsert_conversation(
                    conversation_id,
                    source=source,
                    owner_id=owner_id,
                )
            )
            asyncio.run(
                self.store.save_message(
                    StoredMessage(
                        id=f"secret-message-{index}",
                        conversation_id=conversation_id,
                        role="user",
                        content="不能泄露",
                    )
                )
            )

        with TestClient(self.app) as client:
            for conversation_id, _, _ in conversations:
                with self.subTest(conversation_id=conversation_id):
                    listed = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    )
                    deleted = client.delete(
                        f"/api/conversations/{conversation_id}"
                    )
                    self.assertEqual(listed.status_code, 404)
                    self.assertEqual(deleted.status_code, 404)

            for conversation_id, _, _ in conversations:
                messages = asyncio.run(
                    self.store.list_messages(conversation_id)
                )
                self.assertEqual(len(messages), 1)

    def test_testclient_lifespan_leaves_the_injected_store_open(self):
        with TestClient(self.app):
            self.assertFalse(self.store._closed)

        self.assertFalse(self.store._closed)
