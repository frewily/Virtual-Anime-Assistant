import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app
from core.deployment import DeploymentSettings


def valid_state(**updates):
    payload = {
        "schemaVersion": 1,
        "checkedAt": "2099-08-14T11:59:00Z",
        "overallState": "healthy",
        "vaaState": "ready",
        "onebotState": "connected",
        "backupState": "fresh",
        "latestBackupAt": "2099-08-14T03:00:00Z",
        "consecutiveOnebotFailures": 0,
        "recoveriesInWindow": 0,
        "lastRecoveryAt": None,
        "alertCode": None,
    }
    payload.update(updates)
    return payload


def make_runtime():
    runtime = Mock()
    runtime.application.publisher.subscribe.return_value = Mock()
    runtime.tool_service = None
    runtime.check_scenarios = AsyncMock()
    runtime.aclose = AsyncMock()
    return runtime


class CloudStatusApiTests(unittest.TestCase):
    def test_cloud_status_route_returns_reader_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(valid_state()), encoding="utf-8")
            deployment = DeploymentSettings(
                profile="cloud",
                desktop_monitor_enabled=False,
                cloud_monitor_state_file=path,
            )
            with TestClient(
                create_app(
                    runtime_instance=make_runtime(),
                    deployment_settings=deployment,
                )
            ) as client:
                response = client.get("/api/status/cloud")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["onebotState"], "connected")
        self.assertNotIn("token", response.text.lower())

    def test_desktop_status_is_fixed_unavailable(self):
        deployment = DeploymentSettings(
            profile="desktop",
            desktop_monitor_enabled=True,
            cloud_monitor_state_file=Path("/must/not/be/read.json"),
        )
        with TestClient(
            create_app(
                runtime_instance=make_runtime(),
                deployment_settings=deployment,
            )
        ) as client:
            response = client.get("/api/status/cloud")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"available": False})

    def test_invalid_state_returns_safe_unknown_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"token":"do-not-leak"}', encoding="utf-8")
            deployment = DeploymentSettings(
                profile="cloud",
                desktop_monitor_enabled=False,
                cloud_monitor_state_file=path,
            )
            with TestClient(
                create_app(
                    runtime_instance=make_runtime(),
                    deployment_settings=deployment,
                )
            ) as client:
                response = client.get("/api/status/cloud")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overallState"], "unknown")
        self.assertNotIn("do-not-leak", response.text)


if __name__ == "__main__":
    unittest.main()
