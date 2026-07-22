"""Application runtime shared by HTTP, WebSocket, and background adapters."""

from application.assistant import AssistantApplication
from application.events import ResponsePublisher
from channels.desktop import scenario_result_to_message
from core.monitor import SystemMonitor
from core.scenario import ScenarioEngine
from core.tts import TTSService


class AssistantRuntime:
    def __init__(
        self,
        monitor=None,
        application=None,
        scenario_engine=None,
    ):
        self.monitor = monitor or SystemMonitor()
        if application is None:
            publisher = ResponsePublisher()
            application = AssistantApplication(
                tts=TTSService(),
                publisher=publisher,
            )
        self.application = application
        self.scenario_engine = scenario_engine or ScenarioEngine()
        self._current_window: dict | None = None

    def report_window(self, window: dict) -> None:
        self._current_window = dict(window)

    def current_window(self) -> dict | None:
        return dict(self._current_window) if self._current_window else None

    def status(self) -> dict:
        return self.monitor.get_status()

    async def check_scenarios(self) -> None:
        result = self.scenario_engine.detect(self.status(), self.current_window())
        if result is not None:
            await self.application.handle(scenario_result_to_message(result))


runtime = AssistantRuntime()
