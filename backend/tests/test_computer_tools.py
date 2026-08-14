import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ComputerSnapshot
from computer.tools import EmptyComputerArguments, build_current_state_tool
from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from tools.catalog import ModelToolCatalog
from tools.registry import ToolRegistry
from tools.service import ToolExecutionError


class FakeStateReader:
    def __init__(self, snapshot=None, *, stale=False) -> None:
        self.snapshot = snapshot
        self.stale = stale

    def latest(self):
        if self.snapshot is None:
            return None
        return self.snapshot.model_copy(deep=True)

    def is_stale(self):
        return self.stale


def snapshot() -> ComputerSnapshot:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    return ComputerSnapshot(
        device_id="macbook-main",
        platform=ComputerPlatform.MACOS,
        collected_at=now,
        expires_at=now + timedelta(seconds=45),
        capabilities=frozenset({"system.resources", "system.network"}),
        state={
            "system.resources": {
                "status": "available",
                "cpuPercent": 12,
            },
            "system.network": {
                "status": "unavailable",
                "errorCode": "state_probe_failed",
            },
        },
    )


class ComputerToolTests(unittest.TestCase):
    def test_current_state_is_low_risk_model_tool_scoped_to_desktop(self):
        registry = ToolRegistry()
        registry.register(
            build_current_state_tool(
                FakeStateReader(snapshot()),
                allowed_channels=frozenset({MessageSource.DESKTOP}),
            )
        )
        catalog = ModelToolCatalog(registry)

        desktop = catalog.list(MessageSource.DESKTOP)
        qq = catalog.list(MessageSource.QQ)

        self.assertEqual([tool.name for tool in desktop], ["computer.current_state"])
        self.assertEqual(qq, ())
        definition = registry.require("computer.current_state")
        self.assertIs(definition.risk, ToolRisk.LOW)
        self.assertIn(ToolSource.MODEL, definition.allowed_sources)

    def test_current_state_returns_safe_snapshot_and_stale_partition_summary(self):
        definition = build_current_state_tool(
            FakeStateReader(snapshot(), stale=True),
            allowed_channels=frozenset({MessageSource.QQ}),
        )

        result = asyncio.run(definition.handler(EmptyComputerArguments()))

        self.assertEqual(result["freshness"], "stale")
        self.assertEqual(result["deviceId"], "macbook-main")
        self.assertEqual(result["unavailableCapabilities"], ["system.network"])
        self.assertEqual(result["state"]["system.resources"]["cpuPercent"], 12)

    def test_current_state_missing_snapshot_returns_stable_error(self):
        definition = build_current_state_tool(
            FakeStateReader(),
            allowed_channels=frozenset({MessageSource.DESKTOP}),
        )

        with self.assertRaises(ToolExecutionError) as raised:
            asyncio.run(definition.handler(EmptyComputerArguments()))

        self.assertEqual(
            raised.exception.error_code, "computer_state_unavailable"
        )


if __name__ == "__main__":
    unittest.main()
