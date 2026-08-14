import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ProviderResult
from computer.state import DesktopStateService


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class StaticProvider:
    def __init__(self, capability: str, state: dict) -> None:
        self.capability = capability
        self.state = state
        self.calls = 0

    async def collect(self) -> ProviderResult:
        self.calls += 1
        return ProviderResult(capability=self.capability, state=self.state)


class FailingProvider:
    capability = "system.network"

    async def collect(self) -> ProviderResult:
        raise RuntimeError("private probe details")


class ComputerStateTests(unittest.TestCase):
    def test_collects_providers_concurrently_and_builds_versioned_snapshot(self):
        async def exercise():
            first_started = asyncio.Event()
            second_started = asyncio.Event()

            class CoordinatedProvider:
                def __init__(self, capability, own, peer):
                    self.capability = capability
                    self.own = own
                    self.peer = peer

                async def collect(self):
                    self.own.set()
                    await asyncio.wait_for(self.peer.wait(), timeout=0.2)
                    return ProviderResult(
                        capability=self.capability,
                        state={"status": "available"},
                    )

            clock = FakeClock()
            service = DesktopStateService(
                device_id="macbook-main",
                platform=ComputerPlatform.MACOS,
                providers=(
                    CoordinatedProvider(
                        "system.resources", first_started, second_started
                    ),
                    CoordinatedProvider(
                        "system.power", second_started, first_started
                    ),
                ),
                clock=clock,
            )

            snapshot = await service.collect_once()

            self.assertEqual(snapshot.schema_version, 1)
            self.assertEqual(snapshot.collected_at, clock.value)
            self.assertEqual(
                snapshot.expires_at,
                clock.value + timedelta(seconds=45),
            )
            self.assertEqual(
                snapshot.capabilities,
                frozenset({"system.resources", "system.power"}),
            )

        asyncio.run(exercise())

    def test_provider_failure_isolated_with_stable_error(self):
        async def exercise():
            service = DesktopStateService(
                device_id="macbook-main",
                platform=ComputerPlatform.MACOS,
                providers=(
                    StaticProvider(
                        "system.resources",
                        {"status": "available", "cpuPercent": 12},
                    ),
                    FailingProvider(),
                ),
                clock=FakeClock(),
            )

            snapshot = await service.collect_once()

            self.assertEqual(
                snapshot.state["system.resources"]["status"], "available"
            )
            self.assertEqual(
                snapshot.state["system.network"],
                {
                    "status": "unavailable",
                    "errorCode": "state_provider_failed",
                },
            )
            self.assertNotIn("private", str(snapshot.model_dump()))

        asyncio.run(exercise())

    def test_latest_is_deep_copy_and_becomes_stale_after_15_seconds(self):
        async def exercise():
            clock = FakeClock()
            service = DesktopStateService(
                device_id="macbook-main",
                platform=ComputerPlatform.MACOS,
                providers=(
                    StaticProvider(
                        "system.resources",
                        {"status": "available", "cpuPercent": 12},
                    ),
                ),
                clock=clock,
            )
            await service.collect_once()

            first = service.latest()
            self.assertIsNotNone(first)
            first.state["system.resources"]["cpuPercent"] = 99
            self.assertEqual(
                service.latest().state["system.resources"]["cpuPercent"], 12
            )
            self.assertFalse(service.is_stale())
            clock.advance(15)
            self.assertTrue(service.is_stale())
            clock.advance(30)
            self.assertFalse(service.latest().is_fresh(clock()))

        asyncio.run(exercise())

    def test_run_refreshes_every_five_seconds_without_real_waiting(self):
        async def exercise():
            clock = FakeClock()
            provider = StaticProvider(
                "system.resources", {"status": "available"}
            )
            sleeps = []

            async def fake_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    clock.advance(seconds)
                    return
                raise asyncio.CancelledError

            service = DesktopStateService(
                device_id="macbook-main",
                platform=ComputerPlatform.MACOS,
                providers=(provider,),
                clock=clock,
                sleep=fake_sleep,
            )

            with self.assertRaises(asyncio.CancelledError):
                await service.run()

            self.assertEqual(provider.calls, 2)
            self.assertEqual(sleeps, [5, 5])

        asyncio.run(exercise())

    def test_aclose_cancels_owned_background_task(self):
        async def exercise():
            sleeping = asyncio.Event()
            cancelled = asyncio.Event()

            async def blocking_sleep(_seconds):
                sleeping.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            service = DesktopStateService(
                device_id="macbook-main",
                platform=ComputerPlatform.MACOS,
                providers=(
                    StaticProvider(
                        "system.resources", {"status": "available"}
                    ),
                ),
                clock=FakeClock(),
                sleep=blocking_sleep,
            )

            self.assertTrue(service.start())
            self.assertFalse(service.start())
            await asyncio.wait_for(sleeping.wait(), timeout=0.2)
            await service.aclose()
            await service.aclose()

            self.assertTrue(cancelled.is_set())
            self.assertFalse(service.running)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
