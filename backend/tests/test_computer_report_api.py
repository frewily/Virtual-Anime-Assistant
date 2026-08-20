import asyncio
import hmac
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.app import create_app
from computer.models import ComputerSnapshot
from computer.state import RemoteDeviceStateStore
from core.deployment import DeploymentSettings


TOKEN = "report-token-0123456789abcdefghi"
DEVICE_ID = "macbook-main"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MutableMonotonic:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def snapshot_payload(
    collected_at: datetime,
    *,
    device_id: str = DEVICE_ID,
) -> dict:
    return {
        "schemaVersion": 1,
        "deviceId": device_id,
        "platform": "macos",
        "collectedAt": collected_at.isoformat().replace("+00:00", "Z"),
        "expiresAt": (collected_at + timedelta(seconds=45))
        .isoformat()
        .replace("+00:00", "Z"),
        "capabilities": ["system.resources"],
        "state": {
            "system.resources": {
                "status": "available",
                "cpuPercent": 10,
                "privateProbe": "must-not-appear-in-response-or-log",
            }
        },
    }


class RemoteDeviceStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)
        self.monotonic = MutableMonotonic()
        self.store = RemoteDeviceStateStore(
            clock=self.clock,
            monotonic=self.monotonic,
        )

    def snapshot(self, when: datetime) -> ComputerSnapshot:
        return ComputerSnapshot.model_validate_json(
            json.dumps(snapshot_payload(when))
        )

    def test_store_keeps_only_latest_snapshot_per_device(self):
        first = self.snapshot(self.now - timedelta(seconds=2))
        newer = self.snapshot(self.now - timedelta(seconds=1))

        self.assertTrue(self.store.put(first))
        self.assertFalse(self.store.put(first))
        self.assertTrue(self.store.put(newer))
        self.assertEqual(self.store.device_count, 1)
        self.assertEqual(self.store.latest(DEVICE_ID), newer)

        returned = self.store.latest(DEVICE_ID)
        returned.state["system.resources"]["cpuPercent"] = 99
        self.assertEqual(
            self.store.latest(DEVICE_ID).state["system.resources"]["cpuPercent"],
            10,
        )

        other = ComputerSnapshot.model_validate_json(
            json.dumps(
                snapshot_payload(
                    self.now,
                    device_id="other-mac",
                )
            )
        )
        self.assertTrue(self.store.put(other))
        self.assertEqual(self.store.device_count, 2)
        self.assertEqual(self.store.latest(DEVICE_ID), newer)

    def test_store_marks_device_offline_at_45_seconds(self):
        stored = self.snapshot(self.now)
        self.store.put(stored)

        self.clock.value = self.now + timedelta(seconds=44)
        self.assertFalse(self.store.is_offline(DEVICE_ID))
        self.clock.value = self.now + timedelta(seconds=45)
        self.assertTrue(self.store.is_offline(DEVICE_ID))
        self.assertTrue(self.store.is_offline("unknown-device"))

    def test_receipt_ttl_caps_future_clock_skew_at_45_seconds(self):
        future = self.snapshot(self.now + timedelta(seconds=15))
        self.store.put(future)

        self.clock.value = self.now + timedelta(seconds=44)
        self.monotonic.value += 44
        self.assertFalse(self.store.is_offline(DEVICE_ID))
        self.clock.value = self.now + timedelta(seconds=45)
        self.monotonic.value += 1
        self.assertTrue(self.store.is_offline(DEVICE_ID))

    def test_wall_clock_rollback_cannot_extend_receipt_ttl(self):
        stored = self.snapshot(self.now)
        self.store.put(stored)

        self.clock.value = self.now - timedelta(days=1)
        self.monotonic.value += 45

        self.assertTrue(self.store.is_offline(DEVICE_ID))


class ComputerReportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)
        self.monotonic = MutableMonotonic()
        self.store = RemoteDeviceStateStore(
            clock=self.clock,
            monotonic=self.monotonic,
        )
        self.runtime = SimpleNamespace(computer_remote_state_store=self.store)
        self.cloud_settings = DeploymentSettings(
            profile="cloud",
            desktop_monitor_enabled=False,
            computer_state_report_token=TOKEN,
            computer_default_device_id=DEVICE_ID,
        )
        self.app = create_app(
            runtime_instance=self.runtime,
            deployment_settings=self.cloud_settings,
        )
        self.client = TestClient(self.app)

    def post(self, payload: dict, token: str = TOKEN):
        return self.client.post(
            "/api/computer/state",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_deployment_settings_load_secret_without_exposing_repr(self):
        settings = DeploymentSettings.from_env(
            {
                "ASSISTANT_RUNTIME_PROFILE": "cloud",
                "ASSISTANT_COMPUTER_STATE_REPORT_TOKEN": TOKEN,
                "ASSISTANT_COMPUTER_DEVICE_ID": DEVICE_ID,
            }
        )

        self.assertEqual(settings.computer_state_report_token, TOKEN)
        self.assertEqual(settings.computer_default_device_id, DEVICE_ID)
        self.assertNotIn(TOKEN, repr(settings))

    def test_valid_bearer_accepts_latest_snapshot_with_redacted_response(self):
        payload = snapshot_payload(self.now)
        with self.assertLogs("computer.report_api", level="INFO") as logs:
            response = self.post(payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {"status": "accepted", "deviceId": DEVICE_ID},
        )
        rendered = response.text + "\n" + "\n".join(logs.output)
        self.assertNotIn("must-not-appear", rendered)
        self.assertNotIn(TOKEN, rendered)

    def test_wrong_bearer_is_rejected_with_constant_time_comparison(self):
        with patch(
            "api.computer.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as compare:
            response = self.post(snapshot_payload(self.now), "wrong-token")

        self.assertEqual(response.status_code, 401)
        candidate_digest, expected_digest = compare.call_args.args
        self.assertIsInstance(candidate_digest, bytes)
        self.assertIsInstance(expected_digest, bytes)
        self.assertEqual(len(candidate_digest), 32)
        self.assertEqual(len(expected_digest), 32)
        self.assertIsNone(self.store.latest(DEVICE_ID))

    def test_malformed_bearers_are_stable_401(self):
        from api.computer import _authorize

        class Headers(dict):
            def __init__(self, values: list[str]) -> None:
                super().__init__()
                self._values = values

            def getlist(self, _: str) -> list[str]:
                return list(self._values)

        cases = (
            [],
            ["Bearer "],
            ["Bearer wrong token"],
            ["Bearer café"],
            ["Bearer " + "x" * 257],
            [f"Bearer {TOKEN}", f"Bearer {TOKEN}"],
        )
        for values in cases:
            with self.subTest(values=values):
                request = SimpleNamespace(headers=Headers(list(values)))
                with (
                    patch(
                        "api.computer.hmac.compare_digest",
                        wraps=hmac.compare_digest,
                    ) as compare,
                    self.assertRaises(HTTPException) as raised,
                ):
                    _authorize(request, TOKEN)
                self.assertEqual(raised.exception.status_code, 401)
                candidate_digest, expected_digest = compare.call_args.args
                self.assertEqual(len(candidate_digest), 32)
                self.assertEqual(len(expected_digest), 32)

    def test_cloud_without_report_token_is_explicitly_disabled(self):
        settings = DeploymentSettings(
            profile="cloud",
            desktop_monitor_enabled=False,
            computer_default_device_id=DEVICE_ID,
        )
        app = create_app(
            runtime_instance=self.runtime,
            deployment_settings=settings,
        )

        response = TestClient(app).post(
            "/api/computer/state",
            json=snapshot_payload(self.now),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        self.assertFalse(settings.computer_state_report_enabled)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "computer_state_report_not_configured",
        )

    def test_body_over_32_kib_is_rejected_before_json_parsing(self):
        response = self.client.post(
            "/api/computer/state",
            content=b"{" + b"x" * (32 * 1024),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "snapshot_too_large")

    def test_rejects_unsupported_schema_invalid_device_and_non_utc_time(self):
        cases = []
        unsupported = snapshot_payload(self.now)
        unsupported["schemaVersion"] = 2
        cases.append(unsupported)
        invalid_device = snapshot_payload(self.now)
        invalid_device["deviceId"] = "../other"
        cases.append(invalid_device)
        non_utc = snapshot_payload(self.now)
        local_time = self.now.astimezone(timezone(timedelta(hours=8)))
        non_utc["collectedAt"] = local_time.isoformat()
        non_utc["expiresAt"] = (local_time + timedelta(seconds=45)).isoformat()
        cases.append(non_utc)

        for payload in cases:
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "invalid_snapshot")

    def test_rejects_snapshot_outside_utc_time_window(self):
        for collected_at in (
            self.now - timedelta(seconds=46),
            self.now + timedelta(seconds=16),
        ):
            with self.subTest(collected_at=collected_at):
                response = self.post(snapshot_payload(collected_at))
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["detail"],
                    "snapshot_time_invalid",
                )

    def test_rejects_same_or_older_collected_at(self):
        first = snapshot_payload(self.now - timedelta(seconds=1))
        self.assertEqual(self.post(first).status_code, 202)

        for payload in (first, snapshot_payload(self.now - timedelta(seconds=2))):
            with self.subTest(collectedAt=payload["collectedAt"]):
                response = self.post(payload)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], "snapshot_not_newer")

    def test_desktop_profile_does_not_mount_report_route(self):
        desktop = create_app(
            runtime_instance=self.runtime,
            deployment_settings=DeploymentSettings(
                profile="desktop",
                desktop_monitor_enabled=True,
                computer_state_report_token=TOKEN,
                computer_default_device_id=DEVICE_ID,
            ),
        )
        response = TestClient(desktop).post(
            "/api/computer/state",
            json=snapshot_payload(self.now),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        self.assertEqual(response.status_code, 404)

    def test_report_device_must_match_configured_token_device(self):
        response = self.post(snapshot_payload(self.now, device_id="other-mac"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "device_not_allowed")

    def test_chunked_oversize_is_limited_without_content_length(self):
        class OversizedRequest:
            headers = {"authorization": f"Bearer {TOKEN}"}

            async def stream(self):
                yield b"{" + b"x" * (16 * 1024)
                yield b"x" * (16 * 1024)

        from api.computer import receive_computer_state

        with self.assertRaisesRegex(
            HTTPException,
            "snapshot_too_large",
        ) as caught:
            asyncio.run(
                receive_computer_state(
                    OversizedRequest(),
                    self.store,
                    self.cloud_settings,
                )
            )
        self.assertEqual(getattr(caught.exception, "status_code", None), 413)


if __name__ == "__main__":
    unittest.main()
