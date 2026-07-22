import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.chat import ChatMessage, handle_message
from api.ws import broadcast_to_desktop, is_allowed_origin, parse_client_message
from core.runtime import runtime

router = runtime.router
from core.scenario import ScenarioEngine


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.broadcaster = AsyncMock()
        router.set_ws_broadcaster(self.broadcaster)

    def test_chat_endpoint_routes_a_message_to_the_desktop(self):
        response = asyncio.run(
            handle_message(ChatMessage(source="desktop", senderId="user", content="你好"))
        )

        self.assertEqual(response, {"reply": "主人说得有道理~", "status": "ok"})
        self.broadcaster.assert_awaited_once()


    def test_broadcast_awaits_each_live_websocket(self):
        class Socket:
            def __init__(self):
                self.send_text = AsyncMock()

        from api import ws

        socket = Socket()
        ws._sessions.add(socket)
        try:
            asyncio.run(broadcast_to_desktop('{"type":"action"}'))
        finally:
            ws._sessions.discard(socket)

        socket.send_text.assert_awaited_once_with('{"type":"action"}')

    def test_app_duration_scenario_triggers_after_the_configured_minutes(self):
        engine = ScenarioEngine()
        focus = next(s for s in engine.scenarios if s["id"] == "focus_mode")
        focus["trigger"]["duration"] = 0

        result = engine.detect({"cpu": {"percent": 0}}, {"appName": "VS Code"})

        self.assertIsNotNone(result)
        self.assertEqual(result["expression"], "happy")

    def test_client_interaction_uses_the_shared_message_router(self):
        response = asyncio.run(
            router.handle_client_message({"type": "interaction", "action": "click"})
        )

        self.assertEqual(response["type"], "action")
        self.broadcaster.assert_awaited_once()

    def test_unknown_client_message_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported message type"):
            asyncio.run(router.handle_client_message({"type": "unknown"}))

    def test_websocket_message_must_be_a_json_object(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_client_message("[]")

    def test_websocket_origin_is_restricted_to_local_electron(self):
        self.assertTrue(is_allowed_origin(None))
        self.assertTrue(is_allowed_origin("file://"))
        self.assertTrue(is_allowed_origin("null"))
        self.assertFalse(is_allowed_origin("https://example.com"))
