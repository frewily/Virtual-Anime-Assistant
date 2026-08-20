"""In-memory collection and freshness tracking for computer state."""

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from computer.capabilities import StateProvider
from computer.models import ComputerPlatform, ComputerSnapshot, ProviderResult


_REFRESH_INTERVAL_SECONDS = 5
_LOCAL_STALE_AFTER = timedelta(seconds=15)
_SNAPSHOT_TTL = timedelta(seconds=45)


class DesktopStateService:
    """Collects fixed providers concurrently and retains only the latest snapshot."""

    def __init__(
        self,
        *,
        device_id: str,
        platform: ComputerPlatform,
        providers: Sequence[StateProvider],
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not providers:
            raise ValueError("computer state providers must not be empty")
        capabilities = tuple(self._provider_capability(item) for item in providers)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("computer state provider capability is duplicated")
        self.device_id = device_id
        self.platform = platform
        self.providers = tuple(providers)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._latest: ComputerSnapshot | None = None
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _provider_capability(provider: StateProvider) -> str:
        capability = getattr(provider, "capability", None)
        if not isinstance(capability, str) or not capability:
            raise TypeError("computer state provider capability is invalid")
        return capability

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("computer state clock must include timezone")
        return value.astimezone(timezone.utc)

    async def collect_once(self) -> ComputerSnapshot:
        results = await asyncio.gather(
            *(provider.collect() for provider in self.providers),
            return_exceptions=True,
        )
        state: dict[str, dict] = {}
        for provider, result in zip(self.providers, results, strict=True):
            capability = self._provider_capability(provider)
            if isinstance(result, BaseException):
                state[capability] = {
                    "status": "unavailable",
                    "errorCode": "state_provider_failed",
                }
            elif not isinstance(result, ProviderResult) or (
                result.capability != capability
            ):
                state[capability] = {
                    "status": "unavailable",
                    "errorCode": "state_provider_invalid",
                }
            else:
                state[capability] = dict(result.state)

        collected_at = self._now()
        snapshot = ComputerSnapshot(
            device_id=self.device_id,
            platform=self.platform,
            collected_at=collected_at,
            expires_at=collected_at + _SNAPSHOT_TTL,
            capabilities=frozenset(state),
            state=state,
        )
        self._latest = snapshot.model_copy(deep=True)
        return snapshot.model_copy(deep=True)

    def latest(self) -> ComputerSnapshot | None:
        if self._latest is None:
            return None
        return self._latest.model_copy(deep=True)

    def is_stale(self) -> bool:
        if self._latest is None:
            return True
        return self._now() - self._latest.collected_at >= _LOCAL_STALE_AFTER

    async def run(self) -> None:
        while True:
            await self.collect_once()
            await self._sleep(_REFRESH_INTERVAL_SECONDS)

    def start(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(
            self.run(),
            name="desktop-computer-state",
        )
        return True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def aclose(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None


class RemoteDeviceStateStore:
    """Keep one defensive in-memory snapshot per remotely reporting device."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._snapshots: dict[
            str,
            tuple[ComputerSnapshot, float],
        ] = {}
        self._lock = threading.Lock()

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("remote computer state clock must include timezone")
        return value.astimezone(timezone.utc)

    def _monotonic_now(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("remote computer monotonic clock is invalid")
        return float(value)

    def put(self, snapshot: ComputerSnapshot) -> bool:
        """Atomically replace a device snapshot only when it is newer."""

        if not isinstance(snapshot, ComputerSnapshot):
            raise TypeError("remote computer snapshot is invalid")
        safe_snapshot = snapshot.model_copy(deep=True)
        received_at = self._monotonic_now()
        with self._lock:
            current_entry = self._snapshots.get(snapshot.device_id)
            current = current_entry[0] if current_entry is not None else None
            if (
                current is not None
                and snapshot.collected_at <= current.collected_at
            ):
                return False
            self._snapshots[snapshot.device_id] = (
                safe_snapshot,
                received_at,
            )
        return True

    def latest(self, device_id: str) -> ComputerSnapshot | None:
        with self._lock:
            entry = self._snapshots.get(device_id)
            if entry is None:
                return None
            snapshot, _ = entry
            return snapshot.model_copy(deep=True)

    def latest_fresh(self, device_id: str) -> ComputerSnapshot | None:
        """Return one atomic fresh view bounded by receipt and envelope TTLs."""

        now = self.now()
        monotonic_now = self._monotonic_now()
        with self._lock:
            entry = self._snapshots.get(device_id)
            if entry is None:
                return None
            snapshot, received_at = entry
            receipt_age = monotonic_now - received_at
            if (
                not snapshot.is_fresh(now)
                or receipt_age < 0
                or receipt_age >= _SNAPSHOT_TTL.total_seconds()
            ):
                return None
            return snapshot.model_copy(deep=True)

    def is_offline(self, device_id: str) -> bool:
        return self.latest_fresh(device_id) is None

    @property
    def device_count(self) -> int:
        with self._lock:
            return len(self._snapshots)
