"""In-memory collection and freshness tracking for computer state."""

import asyncio
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
