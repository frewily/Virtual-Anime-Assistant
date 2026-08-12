import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app
from channels.onebot.config import OneBotSettings
from core.deployment import DeploymentSettings


class HealthApiTests(unittest.TestCase):
    def make_client(
        self,
        *,
        enabled: bool = True,
        connected: bool = True,
        configuration_error: str | None = None,
    ) -> TestClient:
        runtime = Mock()
        runtime.store = Mock()
        type(runtime.store).schema_version = PropertyMock(return_value=1)
        runtime.qq_settings = OneBotSettings(
            enabled=enabled,
            access_token="0123456789abcdef",
            allowed_user_ids=frozenset({123456789}),
            configuration_error=configuration_error,
        )
        runtime.qq_connection.connected = connected
        runtime.application.publisher.subscribe.return_value = Mock()
        runtime.tool_service = None
        runtime.check_scenarios = AsyncMock()
        runtime.aclose = AsyncMock()
        return TestClient(
            create_app(
                runtime_instance=runtime,
                deployment_settings=DeploymentSettings(
                    profile="cloud",
                    desktop_monitor_enabled=False,
                ),
            )
        )

    def test_live_health_is_minimal(self):
        with self.make_client() as client:
            response = client.get("/api/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_ready_health_checks_sqlite_without_exposing_details(self):
        with self.make_client() as client:
            response = client.get("/api/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    def test_onebot_health_never_exposes_credentials_or_ids(self):
        with self.make_client() as client:
            response = client.get("/api/health/onebot")

        payload = response.json()
        serialized = json.dumps(payload)
        self.assertEqual(payload, {"status": "connected"})
        self.assertNotIn("0123456789abcdef", serialized)
        self.assertNotIn("123456789", serialized)

    def test_onebot_health_reports_all_safe_states(self):
        cases = (
            ({"enabled": False}, "disabled"),
            ({"configuration_error": "qq_misconfigured"}, "misconfigured"),
            ({"connected": False}, "disconnected"),
        )
        for options, expected in cases:
            with self.subTest(expected=expected), self.make_client(**options) as client:
                response = client.get("/api/health/onebot")
            self.assertEqual(response.json(), {"status": expected})

    def test_ready_health_returns_fixed_503_on_storage_error(self):
        runtime = Mock()
        runtime.store = Mock()
        type(runtime.store).schema_version = PropertyMock(
            side_effect=RuntimeError("private database path")
        )
        runtime.qq_settings = OneBotSettings()
        runtime.qq_connection.connected = False
        runtime.application.publisher.subscribe.return_value = Mock()
        runtime.tool_service = None
        runtime.check_scenarios = AsyncMock()
        runtime.aclose = AsyncMock()

        with TestClient(
            create_app(
                runtime_instance=runtime,
                deployment_settings=DeploymentSettings(
                    profile="cloud",
                    desktop_monitor_enabled=False,
                ),
            )
        ) as client:
            response = client.get("/api/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})
        self.assertNotIn("private", response.text)


if __name__ == "__main__":
    unittest.main()
