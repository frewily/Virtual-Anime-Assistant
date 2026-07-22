import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import app


class ApiIntegrationTests(unittest.TestCase):
    def test_status_endpoint_returns_system_metrics(self):
        with TestClient(app) as client:
            response = client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("cpu", response.json())
        self.assertIn("memory", response.json())

    def test_websocket_interaction_is_dispatched_and_broadcast(self):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                websocket.send_json({"type": "interaction", "action": "click"})
                response = websocket.receive_json()

        self.assertEqual(response["type"], "action")
        self.assertEqual(response["motion"], "tap_body")

    def test_invalid_websocket_payload_returns_error(self):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/avatar") as websocket:
                websocket.send_text("[]")
                response = websocket.receive_json()

        self.assertEqual(response["type"], "error")
        self.assertIn("JSON object", response["message"])

    def test_chat_http_contract_remains_compatible(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/message",
                json={
                    "source": "desktop",
                    "senderId": "user-1",
                    "content": "你好",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "主人说得有道理~", "status": "ok"},
        )
