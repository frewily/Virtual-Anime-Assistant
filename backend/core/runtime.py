"""Application runtime shared by HTTP, WebSocket, and background adapters."""

import asyncio
import inspect
import threading

from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.model_tools import ModelToolOrchestrator
from channels.desktop import scenario_result_to_message
from channels.onebot.channel import OneBotChannel
from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from core.monitor import SystemMonitor
from core.scenario import ScenarioEngine
from core.tts import TTSService
from infrastructure.database_config import DatabaseSettings
from infrastructure.sqlite_store import SqliteStore
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway
from llm.openai_compatible import OpenAICompatibleGateway
from tools.builtin import build_builtin_registry
from tools.catalog import ModelToolCatalog
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
        qq_settings: OneBotSettings | None = None,
        qq_connection: OneBotConnectionManager | None = None,
        qq_channel: OneBotChannel | None = None,
    ):
        self._owned_resources: list[tuple[str, object]] = []
        self._resource_closed: dict[str, bool] = {}
        self._closed = False
        self.model_tool_catalog = None
        self.model_tool_orchestrator = None
        self._llm = None
        self._tts = None

        try:
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
                if store is None:
                    store = SqliteStore(database.database_path)
                    self._register_owned("store", store)
                self.store = store
                self.tool_registry = (
                    tool_registry
                    or getattr(tool_service, "registry", None)
                    or build_builtin_registry()
                )
                self.tool_service = tool_service or ToolExecutionService(
                    registry=self.tool_registry,
                    repository=self.store,
                )
                tools_enabled = (
                    settings.enabled
                    and settings.tool_calling_enabled
                )
                if tools_enabled:
                    self.model_tool_catalog = ModelToolCatalog(
                        self.tool_registry
                    )
                llm = (
                    OpenAICompatibleGateway(settings)
                    if settings.enabled
                    else DemoLanguageModelGateway()
                )
                self._llm = llm
                self._register_owned("llm", llm)
                if tools_enabled:
                    self.model_tool_orchestrator = ModelToolOrchestrator(
                        gateway=llm,
                        catalog=self.model_tool_catalog,
                        tool_service=self.tool_service,
                        enabled=True,
                    )
                tts = TTSService()
                self._tts = tts
                self._register_owned("tts", tts)
                context_builder = ConversationContextBuilder(
                    settings.max_context_messages,
                    settings.max_context_chars,
                )
                publisher = ResponsePublisher()
                application = AssistantApplication(
                    tts=tts,
                    llm=llm,
                    store=store,
                    context_builder=context_builder,
                    publisher=publisher,
                    model_orchestrator=self.model_tool_orchestrator,
                )
            else:
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

            self.application = application
            self.qq_settings = qq_settings or OneBotSettings.from_env()
            if qq_connection is None:
                qq_connection = OneBotConnectionManager(
                    action_timeout_seconds=(
                        self.qq_settings.action_timeout_seconds
                    ),
                )
                self._register_owned("qq_connection", qq_connection)
            self.qq_connection = qq_connection
            if qq_channel is None:
                qq_channel = OneBotChannel(
                    application=self.application,
                    settings=self.qq_settings,
                    connection=self.qq_connection,
                )
                self._register_owned("qq_channel", qq_channel)
            self.qq_channel = qq_channel
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
            self._closed = all(self._resource_closed.values())
        except BaseException:
            self._rollback_initialization()
            raise

    def _register_owned(self, name: str, resource: object) -> None:
        self._owned_resources.append((name, resource))
        self._resource_closed[name] = False

    def _rollback_initialization(self) -> None:
        for name, resource in reversed(self._owned_resources):
            if self._resource_closed[name]:
                continue
            try:
                if name == "store":
                    resource._close_sync()
                else:
                    self._close_resource_sync(resource)
            except BaseException:
                continue
            self._resource_closed[name] = True

    @classmethod
    def _close_resource_sync(cls, resource: object) -> None:
        close = cls._close_method(resource)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            cls._await_cleanup(result)

    @staticmethod
    async def _close_resource(name: str, resource: object) -> None:
        close = AssistantRuntime._close_method(
            resource,
            prefer_close=name == "store",
        )
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _close_method(resource: object, *, prefer_close: bool = False):
        if prefer_close:
            close = getattr(resource, "close", None)
            if close is not None:
                return close
        close = getattr(resource, "aclose", None)
        if close is not None:
            return close
        return getattr(resource, "close", None)

    @staticmethod
    def _await_cleanup(awaitable) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(awaitable)
            return

        errors: list[BaseException] = []

        def run() -> None:
            try:
                asyncio.run(awaitable)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        if errors:
            raise errors[0]

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
        first_error: BaseException | None = None
        for name, resource in reversed(self._owned_resources):
            if self._resource_closed[name]:
                continue
            try:
                await self._close_resource(name, resource)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                self._resource_closed[name] = True

        self._closed = all(self._resource_closed.values())
        if first_error is not None:
            raise first_error
