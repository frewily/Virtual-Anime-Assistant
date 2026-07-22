"""Application runtime shared by HTTP, WebSocket, and background adapters."""

from core.monitor import SystemMonitor
from core.router import MessageRouter


class AssistantRuntime:
    def __init__(self, monitor=None, router=None):
        self.monitor = monitor or SystemMonitor()
        self.router = router or MessageRouter()
        self._current_window: dict | None = None

    def report_window(self, window: dict) -> None:
        self._current_window = dict(window)

    def current_window(self) -> dict | None:
        return dict(self._current_window) if self._current_window else None

    def status(self) -> dict:
        return self.monitor.get_status()

    async def check_scenarios(self) -> None:
        await self.router.handle_scenario_check(self.status(), self.current_window())


runtime = AssistantRuntime()
