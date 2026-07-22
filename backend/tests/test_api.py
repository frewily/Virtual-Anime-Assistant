import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.chat import ChatMessage, handle_message
from api.ws import broadcast_to_desktop, is_allowed_origin, parse_client_message
from channels.desktop import client_payload_to_message
from core.runtime import runtime
from core.scenario import ScenarioEngine
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.subscriber = AsyncMock()
        self.unsubscribe = runtime.application.publisher.subscribe(self.subscriber)

    def tearDown(self):
        self.unsubscribe()

    def test_chat_endpoint_routes_a_message_to_the_desktop(self):
        response = asyncio.run(
            handle_message(ChatMessage(source="desktop", senderId="user", content="你好"))
        )

        self.assertEqual(response, {"reply": "主人说得有道理~", "status": "ok"})
        self.subscriber.assert_awaited_once()

    def test_broadcast_serializes_response_for_each_live_websocket(self):
        class Socket:
            def __init__(self):
                self.send_text = AsyncMock()

        from api import ws

        socket = Socket()
        response = AssistantResponse(
            correlation_id="message-1",
            conversation_id="desktop:local-user",
            kind=ResponseKind.ACTION,
            avatar=AvatarCue(expression="surprised", motion="tap_body"),
        )
        ws._sessions.add(socket)
        try:
            asyncio.run(broadcast_to_desktop(response))
        finally:
            ws._sessions.discard(socket)

        payload = json.loads(socket.send_text.await_args.args[0])
        self.assertEqual(payload["type"], "action")
        self.assertEqual(payload["correlationId"], "message-1")
        self.assertEqual(payload["motion"], "tap_body")

    def test_app_duration_scenario_triggers_after_the_configured_minutes(self):
        engine = ScenarioEngine()
        focus = next(s for s in engine.scenarios if s["id"] == "focus_mode")
        focus["trigger"]["duration"] = 0

        result = engine.detect({"cpu": {"percent": 0}}, {"appName": "VS Code"})

        self.assertIsNotNone(result)
        self.assertEqual(result["expression"], "happy")

    def test_client_interaction_uses_the_shared_application(self):
        response = asyncio.run(
            runtime.application.handle(
                client_payload_to_message({"type": "interaction", "action": "click"})
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
