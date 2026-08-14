import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    ChannelPolicy,
)
from computer.models import (
    ComputerPlatform,
    ComputerSnapshot,
    ModelAccess,
    ProviderResult,
)
from domain.messages import MessageSource
from domain.tools import ToolRisk


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FakeProvider:
    async def collect(self) -> ProviderResult:
        return ProviderResult(
            capability="system.resources",
            state={"status": "available", "cpuPercent": 20},
        )


def definition(
    name: str = "system.resources",
    *,
    channels: frozenset[MessageSource] = frozenset(
        {MessageSource.DESKTOP}
    ),
    profiles: frozenset[str] = frozenset({"desktop"}),
) -> CapabilityDefinition:
    return CapabilityDefinition(
        name=name,
        title="读取系统资源",
        platforms=frozenset({ComputerPlatform.MACOS}),
        runtime_profiles=profiles,
        risk=ToolRisk.LOW,
        model_access=ModelAccess.READ_ONLY,
        allowed_channels=channels,
        arguments_model=EmptyArguments,
        provider=FakeProvider(),
    )


class ComputerModelTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    def snapshot(self, **updates) -> ComputerSnapshot:
        values = {
            "device_id": "macbook-main",
            "platform": ComputerPlatform.MACOS,
            "collected_at": self.now,
            "expires_at": self.now + timedelta(seconds=45),
            "capabilities": frozenset({"system.resources"}),
            "state": {
                "system.resources": {
                    "status": "available",
                    "cpuPercent": 20,
                }
            },
        }
        values.update(updates)
        return ComputerSnapshot(**values)

    def test_snapshot_is_versioned_strict_and_expires_at_45_seconds(self):
        snapshot = self.snapshot()

        self.assertEqual(snapshot.schema_version, 1)
        self.assertTrue(snapshot.is_fresh(self.now + timedelta(seconds=44)))
        self.assertFalse(snapshot.is_fresh(self.now + timedelta(seconds=45)))
        with self.assertRaises(ValidationError):
            self.snapshot(device_id="MacBook Main")
        with self.assertRaises(ValidationError):
            self.snapshot(expires_at=self.now + timedelta(seconds=46))
        with self.assertRaises(ValidationError):
            self.snapshot(collected_at=self.now.replace(tzinfo=None))

    def test_snapshot_accepts_unknown_extension_fields_but_closes_state_keys(self):
        payload = self.snapshot().model_dump(mode="json", by_alias=True)
        payload["futureMetadata"] = {"ignored": True}

        restored = ComputerSnapshot.model_validate_json(json.dumps(payload))

        self.assertEqual(restored.device_id, "macbook-main")
        with self.assertRaises(ValidationError):
            self.snapshot(
                capabilities=frozenset({"system.resources", "system.power"})
            )

    def test_provider_result_requires_matching_stable_capability(self):
        result = ProviderResult(
            capability="system.resources",
            state={"status": "available"},
        )

        self.assertEqual(result.capability, "system.resources")
        with self.assertRaises(ValidationError):
            ProviderResult(capability="Bad Name", state={})

    def test_registry_rejects_duplicates_and_policy_filters_all_dimensions(self):
        registry = CapabilityRegistry()
        registry.register(definition())
        registry.register(
            definition(
                "system.power",
                channels=frozenset({MessageSource.DESKTOP, MessageSource.QQ}),
                profiles=frozenset({"desktop", "cloud"}),
            )
        )

        with self.assertRaises(ValueError):
            registry.register(definition())

        policy = ChannelPolicy(registry)
        desktop = policy.list_for(
            platform=ComputerPlatform.MACOS,
            runtime_profile="desktop",
            channel=MessageSource.DESKTOP,
        )
        cloud_qq = policy.list_for(
            platform=ComputerPlatform.MACOS,
            runtime_profile="cloud",
            channel=MessageSource.QQ,
        )

        self.assertEqual(
            [item.name for item in desktop],
            ["system.power", "system.resources"],
        )
        self.assertEqual([item.name for item in cloud_qq], ["system.power"])


if __name__ == "__main__":
    unittest.main()
