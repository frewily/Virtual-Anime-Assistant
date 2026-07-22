import asyncio
import inspect
import logging
import sys
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _get_impl():
    if sys.platform == "darwin":
        from agent.macos import get_foreground_app

        return get_foreground_app
    if sys.platform == "win32":
        from agent.windows import get_foreground_app

        return get_foreground_app
    return lambda: None


class ForegroundWindowMonitor:
    def __init__(self, get_app: Callable, on_change: Callable):
        self._get_app = get_app
        self._on_change = on_change
        self._last_app: dict | None = None

    async def poll_once(self) -> dict | None:
        app = await asyncio.to_thread(self._get_app)
        if not app or app == self._last_app:
            return None

        result = self._on_change(dict(app))
        if inspect.isawaitable(result):
            await result
        self._last_app = dict(app)
        logger.info("Foreground application changed: %s", app.get("appName", "unknown"))
        return app

    async def run(self, interval_seconds: float = 3) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(interval_seconds)


async def run(on_window_change: Callable) -> None:
    await ForegroundWindowMonitor(_get_impl(), on_window_change).run()
