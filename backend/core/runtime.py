"""Application runtime shared by HTTP, WebSocket, and background adapters."""

import inspect

from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from channels.desktop import scenario_result_to_message
from core.monitor import SystemMonitor
from core.scenario import ScenarioEngine
from core.tts import TTSService
from infrastructure.database_config import DatabaseSettings
from infrastructure.sqlite_store import SqliteStore
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway
from llm.openai_compatible import OpenAICompatibleGateway
from tools.builtin import build_builtin_registry
from tools.registry import ToolRegistry
from tools.service import ToolExecutionService


class AssistantRuntime:
    def __init__(
        self,
        monitor=None,
        application=None,
        scenario_engine=None,
        store=None,
        llm_settings: LLMSettings | None = None,
        database_settings: DatabaseSettings | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_service: ToolExecutionService | None = None,
    ):
        self.monitor = monitor if monitor is not None else SystemMonitor()
        self.scenario_engine = (
            scenario_engine
            if scenario_engine is not None
            else ScenarioEngine()
        )
        if application is None:
            settings = llm_settings or LLMSettings.from_env()
            database = (
                database_settings or DatabaseSettings.from_env()
                if store is None
                else None
            )
            llm = (
                OpenAICompatibleGateway(settings)
                if settings.enabled
                else DemoLanguageModelGateway()
            )
            tts = TTSService()
            context_builder = ConversationContextBuilder(
                settings.max_context_messages,
                settings.max_context_chars,
            )
            publisher = ResponsePublisher()
            if store is None:
                store = SqliteStore(database.database_path)
            application = AssistantApplication(
                tts=tts,
                llm=llm,
                store=store,
                context_builder=context_builder,
                publisher=publisher,
            )
        self.application = application
        self.store = (
            store
            if store is not None
            else getattr(application, "store", None)
        )
        self.tool_registry = (
            tool_registry
            or getattr(tool_service, "registry", None)
            or build_builtin_registry()
        )
        self.tool_service = tool_service
        if self.tool_service is None and self.store is not None:
            self.tool_service = ToolExecutionService(
                registry=self.tool_registry,
                repository=self.store,
            )
        if llm_settings is not None:
            self.llm_mode = (
                "configured" if llm_settings.enabled else "demo"
            )
        else:
            self.llm_mode = (
                "configured"
                if isinstance(
                    getattr(application, "llm", None),
                    OpenAICompatibleGateway,
                )
                else "demo"
            )
        self._current_window: dict | None = None
        self._closed = False

    def report_window(self, window: dict) -> None:
        self._current_window = dict(window)

    def current_window(self) -> dict | None:
        return dict(self._current_window) if self._current_window else None

    def status(self) -> dict:
        monitored = self.monitor.get_status()
        status: dict = {}
        for section, allowed_fields in (
            ("cpu", ("percent", "cores")),
            ("memory", ("total", "used", "percent")),
        ):
            monitored_section = monitored.get(section)
            if not isinstance(monitored_section, dict):
                continue
            filtered = {
                field: monitored_section[field]
                for field in allowed_fields
                if field in monitored_section
            }
            if filtered:
                status[section] = filtered
        if "uptime" in monitored:
            status["uptime"] = monitored["uptime"]
        status["assistant"] = {"llmMode": self.llm_mode}
        return status

    async def check_scenarios(self) -> None:
        result = self.scenario_engine.detect(self.status(), self.current_window())
        if result is not None:
            await self.application.handle(scenario_result_to_message(result))

    async def aclose(self) -> None:
        if self._closed:
            return
        close = getattr(self.store, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._closed = True
