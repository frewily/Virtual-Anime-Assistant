import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app
from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from channels.desktop import LOCAL_USER
from core.runtime import AssistantRuntime
from infrastructure.sqlite_store import SqliteStore
from llm.errors import ModelServiceError
from llm.models import ModelReply
from memory.models import MemoryItem, StoredMessage


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SqliteStore(Path(self.directory.name) / "assistant.db")
        self.llm = Mock()
        self.llm.model_name = "fake-model"
        self.llm.complete = AsyncMock(
            return_value=ModelReply(
                text="主人说得有道理~",
                model="fake-model",
            )
        )
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
        self.app = create_app(runtime_instance=self.runtime)

    def tearDown(self):
        asyncio.run(self.runtime.aclose())
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

    def test_chat_model_error_is_a_safe_503(self):
        self.llm.complete.side_effect = ModelServiceError(
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

    def test_testclient_lifespan_closes_the_injected_store(self):
        with TestClient(self.app):
            self.assertFalse(self.store._closed)

        self.assertTrue(self.store._closed)
