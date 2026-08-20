"""Application runtime shared by HTTP, WebSocket, and background adapters."""

import asyncio
import inspect
import sys
import threading
from collections.abc import Callable

from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.model_tools import ModelToolOrchestrator
from channels.desktop import scenario_result_to_message
from channels.onebot.channel import OneBotChannel
from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from computer.macos import MacOSActionProvider, build_macos_state_providers
from computer.models import ComputerPlatform, ComputerSnapshot
from computer.reporter import ComputerStateReporter
from computer.state import DesktopStateService, RemoteDeviceStateStore
from computer.tools import build_current_state_tool, build_macos_action_tools
from core.deployment import DeploymentSettings
from core.monitor import SystemMonitor
from core.scenario import ScenarioEngine
from core.tts import TTSService
from domain.messages import MessageSource
from infrastructure.database_config import DatabaseSettings
from infrastructure.sqlite_store import SqliteStore
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway
from llm.openai_compatible import OpenAICompatibleGateway
from settings.resolver import RuntimeSettings
from tools.builtin import build_builtin_registry
from tools.catalog import ModelToolCatalog
from tools.registry import ToolRegistry
from tools.service import ToolExecutionError, ToolExecutionService


class _DefaultRemoteStateReader:
    def __init__(
        self,
        store: RemoteDeviceStateStore,
        device_id: str,
    ) -> None:
        self._store = store
        self._device_id = device_id

    def latest(self) -> ComputerSnapshot:
        snapshot = self._store.latest_fresh(self._device_id)
        if snapshot is None:
            raise ToolExecutionError("device_offline")
        return snapshot

    def is_stale(self) -> bool:
        return False


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
        runtime_settings: RuntimeSettings | None = None,
        computer_state_service: DesktopStateService | None = None,
        computer_state_enabled: bool | None = None,
        computer_actions_enabled: bool | None = None,
        computer_remote_report_enabled: bool | None = None,
        computer_state_reporter: ComputerStateReporter | None = None,
        computer_platform: str | None = None,
        deployment_settings: DeploymentSettings | None = None,
        computer_remote_state_store: RemoteDeviceStateStore | None = None,
        confirmation_client_online: Callable[[], bool] | None = None,
        macos_action_provider: MacOSActionProvider | None = None,
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
            self.deployment_settings = (
                deployment_settings or DeploymentSettings.from_env()
            )
            self.scenario_engine = (
                scenario_engine
                if scenario_engine is not None
                else ScenarioEngine()
            )
            computer_settings = (
                runtime_settings.computer
                if runtime_settings is not None
                else None
            )
            self.computer_state_enabled = (
                computer_state_enabled
                if computer_state_enabled is not None
                else bool(computer_settings and computer_settings.state_enabled)
            )
            self.computer_actions_enabled = (
                computer_actions_enabled
                if computer_actions_enabled is not None
                else bool(computer_settings and computer_settings.actions_enabled)
            )
            self.computer_remote_report_enabled = (
                computer_remote_report_enabled
                if computer_remote_report_enabled is not None
                else bool(
                    computer_settings
                    and computer_settings.remote_report_enabled
                )
            )
            if not self.computer_state_enabled and (
                self.computer_actions_enabled
                or self.computer_remote_report_enabled
            ):
                raise ValueError(
                    "computer state must be enabled for dependent capabilities"
                )
            if not self.computer_remote_report_enabled:
                computer_state_reporter = None
            self.computer_platform = computer_platform or (
                "macos" if sys.platform == "darwin" else "other"
            )
            self._computer_platform_value = (
                ComputerPlatform.MACOS
                if self.computer_platform == "macos"
                else None
            )
            self.confirmation_client_online = (
                confirmation_client_online or (lambda: False)
            )
            if (
                computer_state_service is None
                and self.deployment_settings.profile == "desktop"
                and self.computer_state_enabled
                and self.computer_platform == "macos"
            ):
                computer_state_service = DesktopStateService(
                    device_id=(
                        computer_settings.device_id
                        if computer_settings is not None
                        and computer_settings.device_id is not None
                        else self.deployment_settings.computer_default_device_id
                    ),
                    platform=ComputerPlatform.MACOS,
                    providers=build_macos_state_providers(),
                )
            self.computer_state_service = computer_state_service
            if computer_state_service is not None:
                self._register_owned("computer_state", computer_state_service)
            if (
                computer_state_reporter is None
                and self.deployment_settings.profile == "desktop"
                and self.computer_state_enabled
                and self.computer_remote_report_enabled
                and computer_state_service is not None
                and computer_settings is not None
                and computer_settings.relay_target is not None
                and computer_settings.relay_port is not None
                and computer_settings.relay_identity_file is not None
                and computer_settings.relay_known_hosts_file is not None
            ):
                computer_state_reporter = ComputerStateReporter(
                    latest_snapshot=computer_state_service.latest,
                    ssh_target=computer_settings.relay_target,
                    ssh_port=computer_settings.relay_port,
                    identity_file=computer_settings.relay_identity_file,
                    known_hosts_file=(
                        computer_settings.relay_known_hosts_file
                    ),
                )
            self.computer_state_reporter = computer_state_reporter
            if computer_state_reporter is not None:
                self._register_owned(
                    "computer_state_reporter",
                    computer_state_reporter,
                )
            if (
                computer_remote_state_store is None
                and self.deployment_settings.profile == "cloud"
            ):
                computer_remote_state_store = RemoteDeviceStateStore()
            self.computer_remote_state_store = computer_remote_state_store

            if application is None:
                settings = (
                    llm_settings
                    or (
                        runtime_settings.llm
                        if runtime_settings is not None
                        else LLMSettings.from_env()
                    )
                )
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
                self._register_computer_state_tool()
                self._register_macos_action_tools(macos_action_provider)
                if tool_service is None:
                    tool_service = ToolExecutionService(
                        registry=self.tool_registry,
                        repository=self.store,
                        confirmation_client_online=(
                            self.confirmation_client_online
                        ),
                        platform=self._computer_platform_value,
                        runtime_profile=self.deployment_settings.profile,
                        allowed_model_tool_names=(
                            frozenset({"computer.current_state"})
                            if self.deployment_settings.profile == "cloud"
                            else None
                        ),
                    )
                    self._register_owned("tool_service", tool_service)
                self.tool_service = tool_service
                tools_enabled = (
                    settings.enabled
                    and settings.tool_calling_enabled
                )
                if tools_enabled:
                    self.model_tool_catalog = ModelToolCatalog(
                        self.tool_registry,
                        platform=self._computer_platform_value,
                        runtime_profile=self.deployment_settings.profile,
                        confirmation_client_online=(
                            self.confirmation_client_online
                        ),
                        allowed_tool_names=(
                            frozenset({"computer.current_state"})
                            if self.deployment_settings.profile == "cloud"
                            else None
                        ),
                    )
                    catalog_source = (
                        MessageSource.QQ
                        if self.deployment_settings.profile == "cloud"
                        else MessageSource.DESKTOP
                    )
                    tools_enabled = bool(
                        self.model_tool_catalog.list(catalog_source)
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
                if runtime_settings is None:
                    tts = TTSService()
                else:
                    tts = TTSService(
                        gpt_sovits_url=(
                            runtime_settings.tts.gpt_sovits_url
                        ),
                        default_voice=(
                            runtime_settings.tts.default_voice_id
                        ),
                        audio_max_age_seconds=(
                            runtime_settings.tts.audio_max_age_seconds
                        ),
                    )
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
                self._register_computer_state_tool()
                self._register_macos_action_tools(macos_action_provider)
                self.tool_service = tool_service
                if self.tool_service is None and self.store is not None:
                    self.tool_service = ToolExecutionService(
                        registry=self.tool_registry,
                        repository=self.store,
                        confirmation_client_online=(
                            self.confirmation_client_online
                        ),
                        platform=self._computer_platform_value,
                        runtime_profile=self.deployment_settings.profile,
                        allowed_model_tool_names=(
                            frozenset({"computer.current_state"})
                            if self.deployment_settings.profile == "cloud"
                            else None
                        ),
                    )
                    self._register_owned("tool_service", self.tool_service)

            self.application = application
            self.qq_settings = (
                qq_settings
                or (
                    runtime_settings.qq
                    if runtime_settings is not None
                    else OneBotSettings.from_env()
                )
            )
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
            configured_settings = llm_settings or (
                runtime_settings.llm
                if runtime_settings is not None
                else None
            )
            if configured_settings is not None:
                self.llm_mode = (
                    "configured" if configured_settings.enabled else "demo"
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

    def _register_computer_state_tool(self) -> None:
        if (
            self.deployment_settings.profile == "cloud"
            and self.computer_remote_state_store is not None
        ):
            self.tool_registry.register(
                build_current_state_tool(
                    _DefaultRemoteStateReader(
                        self.computer_remote_state_store,
                        self.deployment_settings.computer_default_device_id,
                    ),
                    allowed_channels=frozenset({MessageSource.QQ}),
                )
            )
            return
        if (
            not self.computer_state_enabled
            or self.computer_state_service is None
        ):
            return
        self.tool_registry.register(
            build_current_state_tool(
                self.computer_state_service,
                allowed_channels=frozenset({MessageSource.DESKTOP}),
            )
        )

    def _register_macos_action_tools(
        self,
        provider: MacOSActionProvider | None,
    ) -> None:
        if (
            self.deployment_settings.profile != "desktop"
            or self._computer_platform_value is not ComputerPlatform.MACOS
            or not self.computer_actions_enabled
        ):
            return
        existing = {definition.name for definition in self.tool_registry.list()}
        for definition in build_macos_action_tools(
            provider or MacOSActionProvider()
        ):
            if definition.name not in existing:
                self.tool_registry.register(definition)

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

    def start_computer_state(self, *, profile: str) -> bool:
        if (
            profile != "desktop"
            or not self.computer_state_enabled
            or self.computer_platform != "macos"
            or self.computer_state_service is None
        ):
            return False
        state_started = self.computer_state_service.start()
        reporter_started = (
            self.computer_state_reporter.start()
            if self.computer_state_reporter is not None
            else False
        )
        return state_started or reporter_started

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
