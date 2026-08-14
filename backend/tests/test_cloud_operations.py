import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cloud_operations import CloudOperationsReader


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def valid_state(**updates):
    payload = {
        "schemaVersion": 1,
        "checkedAt": "2026-08-14T11:59:00Z",
        "overallState": "healthy",
        "vaaState": "ready",
        "onebotState": "connected",
        "backupState": "fresh",
        "latestBackupAt": "2026-08-14T03:00:00Z",
        "consecutiveOnebotFailures": 0,
        "recoveriesInWindow": 0,
        "lastRecoveryAt": None,
        "alertCode": None,
    }
    payload.update(updates)
    return payload


class ExplodingPath:
    def read_bytes(self):
        raise AssertionError("desktop profile must not read cloud state")


class CloudOperationsTests(unittest.TestCase):
    def snapshot_for(self, content, *, now=NOW):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(content, encoding="utf-8")
            return CloudOperationsReader(
                profile="cloud",
                state_file=path,
                now=lambda: now,
            ).snapshot()

    def test_valid_cloud_state_is_returned_as_safe_camel_case_payload(self):
        payload = self.snapshot_for(json.dumps(valid_state()))

        serialized = payload.model_dump(by_alias=True)
        self.assertTrue(serialized["available"])
        self.assertEqual(serialized["overallState"], "healthy")
        self.assertEqual(serialized["onebotState"], "connected")

    def test_desktop_profile_never_reads_cloud_file(self):
        reader = CloudOperationsReader(
            profile="desktop",
            state_file=ExplodingPath(),
        )

        self.assertEqual(
            reader.snapshot().model_dump(by_alias=True),
            {"available": False},
        )

    def test_invalid_state_fails_closed_without_echoing_content(self):
        cases = (
            "not-json",
            json.dumps({**valid_state(), "token": "private-token-value"}),
            json.dumps({**valid_state(), "schemaVersion": 2}),
            json.dumps({**valid_state(), "recoveriesInWindow": 3}),
            json.dumps({**valid_state(), "checkedAt": "2026-08-14T11:59:00"}),
        )
        for content in cases:
            with self.subTest(content=content[:20]):
                payload = self.snapshot_for(content).model_dump_json()
                self.assertIn('"overallState":"unknown"', payload)
                self.assertNotIn("private-token-value", payload)

    def test_state_older_than_three_minutes_is_unknown(self):
        payload = self.snapshot_for(
            json.dumps(valid_state(checkedAt="2026-08-14T11:56:59Z"))
        )

        self.assertEqual(payload.overall_state, "unknown")
        self.assertEqual(payload.alert_code, "state_invalid")

    def test_missing_state_is_safe_unknown(self):
        reader = CloudOperationsReader(
            profile="cloud",
            state_file=Path("/definitely/missing/cloud-state.json"),
            now=lambda: NOW,
        )

        payload = reader.snapshot()

        self.assertTrue(payload.available)
        self.assertEqual(payload.overall_state, "unknown")
        self.assertEqual(payload.alert_code, "state_invalid")


if __name__ == "__main__":
    unittest.main()
