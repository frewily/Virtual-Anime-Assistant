import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.chat import ChatMessage, handle_message
from api.status import get_status
from api.ws import (
    DesktopWebSocketHub,
    is_allowed_origin,
    parse_client_message,
)
from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from channels.desktop import client_payload_to_message
from core.runtime import AssistantRuntime
from core.scenario import ScenarioEngine
from domain.responses import AssistantResponse, AvatarCue, ResponseKind
from infrastructure.sqlite_store import SqliteStore
from llm.errors import ModelServiceError
from llm.models import ModelReply


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SqliteStore(Path(self.directory.name) / "assistant.db")
        self.llm = Mock()
        self.llm.model_name = "fake-model"
        self.llm.complete = AsyncMock(
            return_value=ModelReply(text="模型回答", model="fake-model")
        )
        self.publisher = ResponsePublisher()
        self.subscriber = AsyncMock()
        self.publisher.subscribe(self.subscriber)
        tts = Mock()
        tts.synthesize = AsyncMock(return_value=None)
        self.application = AssistantApplication(
            tts=tts,
            llm=self.llm,
            store=self.store,
            context_builder=ConversationContextBuilder(20, 12000),
            publisher=self.publisher,
        )
        monitor = Mock()
        monitor.get_status.return_value = {
            "cpu": {"percent": 5},
            "privatePath": "/private/data/assistant.db",
        }
        scenario_engine = Mock()
        scenario_engine.detect.return_value = None
        self.runtime = AssistantRuntime(
            monitor=monitor,
            application=self.application,
            scenario_engine=scenario_engine,
        )

    def tearDown(self):
        asyncio.run(self.runtime.aclose())
        asyncio.run(self.store.close())
        self.directory.cleanup()

    def test_chat_endpoint_routes_a_message_to_the_injected_runtime(self):
        response = asyncio.run(
            handle_message(
                ChatMessage(
                    source="desktop",
                    senderId="local-user",
                    content="你好",
                    messageId="http-message-1",
                ),
                self.runtime,
            )
        )

        self.assertEqual(response, {"reply": "模型回答", "status": "ok"})
        self.assertEqual(self.llm.complete.await_count, 1)
        published = self.subscriber.await_args.args[0]
        self.assertEqual(published.correlation_id, "http-message-1")

    def test_chat_request_rejects_unknown_sources_and_invalid_identifiers(self):
        invalid_payloads = (
            {
                "source": "qq",
                "senderId": "local-user",
                "content": "你好",
            },
            {
                "source": "desktop",
                "senderId": "",
                "content": "你好",
            },
            {
                "source": "desktop",
                "senderId": "local-user",
                "content": "你好",
                "messageId": "",
            },
            {
                "source": "desktop",
                "senderId": "x" * 201,
                "content": "你好",
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    ChatMessage.model_validate(payload)

    def test_chat_request_strips_text_fields_before_validation(self):
        message = ChatMessage.model_validate(
            {
                "source": "desktop",
                "senderId": "  local-user  ",
                "content": "  你好  ",
                "messageId": "  client-message  ",
            }
        )

        self.assertEqual(message.sender_id, "local-user")
        self.assertEqual(message.content, "你好")
        self.assertEqual(message.message_id, "client-message")

    def test_model_error_becomes_503_with_only_safe_application_text(self):
        self.llm.complete.side_effect = ModelServiceError(
            "private https://provider.example?key=secret"
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                handle_message(
                    ChatMessage(
                        source="desktop",
                        senderId="local-user",
                        content="你好",
                    ),
                    self.runtime,
                )
            )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "模型服务暂时不可用，请稍后再试。",
        )
        self.assertNotIn("provider.example", raised.exception.detail)
        self.assertNotIn("secret", raised.exception.detail)

    def test_status_adds_llm_mode_without_configuration_secrets(self):
        status = get_status(self.runtime)

        self.assertEqual(status["assistant"], {"llmMode": "demo"})
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("base_url", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn(str(self.store.database_path), serialized)

    def test_broadcast_serializes_response_for_each_live_websocket(self):
        class Socket:
            def __init__(self):
                self.send_text = AsyncMock()

        hub = DesktopWebSocketHub()
        socket = Socket()
        response = AssistantResponse(
            correlation_id="message-1",
            conversation_id="desktop:local-user",
            kind=ResponseKind.ACTION,
            avatar=AvatarCue(expression="surprised", motion="tap_body"),
        )
        hub.attach(socket)
        asyncio.run(hub.broadcast_response(response))

        payload = json.loads(socket.send_text.await_args.args[0])
        self.assertEqual(payload["type"], "action")
        self.assertEqual(payload["correlationId"], "message-1")
        self.assertEqual(payload["motion"], "tap_body")

    def test_broadcast_json_removes_disconnected_sessions(self):
        class Socket:
            send_text = AsyncMock(side_effect=RuntimeError("closed"))

        disconnected = AsyncMock()
        hub = DesktopWebSocketHub(on_last_disconnect=disconnected)
        socket = Socket()
        hub.attach(socket)

        asyncio.run(hub.broadcast_json({"type": "example"}))

        self.assertEqual(hub.connected_count, 0)
        disconnected.assert_awaited_once_with()

    def test_desktop_hubs_isolate_broadcasts_and_last_disconnects(self):
        class Socket:
            def __init__(self):
                self.send_text = AsyncMock()

        disconnected_a = AsyncMock()
        disconnected_b = AsyncMock()
        hub_a = DesktopWebSocketHub(on_last_disconnect=disconnected_a)
        hub_b = DesktopWebSocketHub(on_last_disconnect=disconnected_b)
        socket_a1 = Socket()
        socket_a2 = Socket()
        socket_b = Socket()
        hub_a.attach(socket_a1)
        hub_a.attach(socket_a2)
        hub_b.attach(socket_b)

        asyncio.run(hub_a.broadcast_json({"type": "only-a"}))

        socket_a1.send_text.assert_awaited_once()
        socket_a2.send_text.assert_awaited_once()
        socket_b.send_text.assert_not_awaited()
        self.assertEqual(hub_a.connected_count, 2)
        self.assertEqual(hub_b.connected_count, 1)

        asyncio.run(hub_a.detach(socket_a1))
        disconnected_a.assert_not_awaited()
        asyncio.run(hub_a.detach(socket_a2))
        disconnected_a.assert_awaited_once_with()
        disconnected_b.assert_not_awaited()
        asyncio.run(hub_a.detach(socket_a2))
        disconnected_a.assert_awaited_once_with()

        asyncio.run(hub_b.detach(socket_b))
        disconnected_b.assert_awaited_once_with()

    def test_app_duration_scenario_triggers_after_the_configured_minutes(self):
        engine = ScenarioEngine()
        focus = next(s for s in engine.scenarios if s["id"] == "focus_mode")
        focus["trigger"]["duration"] = 0

        result = engine.detect({"cpu": {"percent": 0}}, {"appName": "VS Code"})

        self.assertIsNotNone(result)
        self.assertEqual(result["expression"], "happy")

    def test_client_interaction_uses_the_shared_application(self):
        response = asyncio.run(
            self.runtime.application.handle(
                client_payload_to_message(
                    {"type": "interaction", "action": "click"}
                )
            )
        )

        self.assertEqual(response.kind, ResponseKind.ACTION)
        self.subscriber.assert_awaited_once_with(response)

    def test_unknown_client_message_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported message type"):
            client_payload_to_message({"type": "unknown"})

    def test_websocket_message_must_be_a_json_object(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_client_message("[]")

    def test_websocket_origin_is_restricted_to_local_electron(self):
        self.assertTrue(is_allowed_origin(None))
        self.assertTrue(is_allowed_origin("file://"))
        self.assertTrue(is_allowed_origin("null"))
        self.assertFalse(is_allowed_origin("https://example.com"))
