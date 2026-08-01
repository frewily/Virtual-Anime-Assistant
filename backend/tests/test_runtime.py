import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel, RootModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.monitor import ForegroundWindowMonitor
from api.app import create_app, lifespan
from application.assistant import AssistantApplication
from channels.onebot.channel import OneBotChannel
from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from core.runtime import AssistantRuntime
from domain.tools import ToolRisk, ToolSource
from infrastructure.database_config import DatabaseSettings
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway
from llm.openai_compatible import OpenAICompatibleGateway
from tools.catalog import ModelToolCatalog
from tools.registry import ToolDefinition, ToolRegistry
from tools.service import ToolExecutionService


def llm_settings(
    *,
    enabled: bool,
    tool_calling_enabled: bool = False,
) -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        base_url="https://llm.example/v1" if enabled else None,
        api_key="private-api-key" if enabled else None,
        model="configured-model" if enabled else None,
        tool_calling_enabled=tool_calling_enabled,
        timeout_seconds=10,
        max_context_messages=8,
        max_context_chars=5000,
    )


class RuntimeToolArguments(BaseModel):
    value: str


class InvalidRuntimeToolArguments(RootModel[str]):
    pass


async def runtime_tool_handler(_: BaseModel) -> dict:
    return {}


def runtime_tool_definition(
    name: str,
    *,
    risk: ToolRisk = ToolRisk.LOW,
    arguments_model: type[BaseModel] = RuntimeToolArguments,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        title=name,
        arguments_model=arguments_model,
        risk=risk,
        impact="测试影响",
        timeout_seconds=1,
        cancellable=True,
        handler=runtime_tool_handler,
        allowed_sources=frozenset({ToolSource.MODEL}),
    )


class RuntimeTests(unittest.TestCase):
    def test_runtime_builds_side_effect_free_disabled_qq_components(self):
        application = Mock(spec=AssistantApplication)

        runtime = AssistantRuntime(
            application=application,
            qq_settings=OneBotSettings(),
        )

        self.assertFalse(runtime.qq_settings.enabled)
        self.assertIsInstance(
            runtime.qq_connection,
            OneBotConnectionManager,
        )
        self.assertIsInstance(runtime.qq_channel, OneBotChannel)
        self.assertFalse(runtime.qq_connection.connected)
        asyncio.run(runtime.aclose())

    def test_explicit_qq_dependencies_are_preserved(self):
        settings = OneBotSettings(
            enabled=True,
            access_token="0123456789abcdef",
            allowed_group_ids=frozenset({789}),
        )
        connection = Mock()
        connection.aclose = AsyncMock()
        channel = Mock()

        runtime = AssistantRuntime(
            application=Mock(spec=AssistantApplication),
            qq_settings=settings,
            qq_connection=connection,
            qq_channel=channel,
        )

        self.assertIs(runtime.qq_settings, settings)
        self.assertIs(runtime.qq_connection, connection)
        self.assertIs(runtime.qq_channel, channel)
        asyncio.run(runtime.aclose())
        connection.aclose.assert_not_awaited()

    def test_misconfigured_qq_does_not_block_core_runtime(self):
        settings = OneBotSettings(
            enabled=True,
            configuration_error="qq_misconfigured",
        )
        application = Mock(spec=AssistantApplication)

        runtime = AssistantRuntime(
            application=application,
            qq_settings=settings,
        )

        self.assertIs(runtime.application, application)
        self.assertEqual(
            runtime.qq_settings.configuration_error,
            "qq_misconfigured",
        )
        asyncio.run(runtime.aclose())

    def test_close_does_not_touch_explicit_qq_or_store_dependencies(self):
        events: list[str] = []
        connection = Mock()
        store = Mock()

        async def close_qq():
            events.append("qq")

        async def close_store():
            events.append("store")

        connection.aclose = AsyncMock(side_effect=close_qq)
        store.close = AsyncMock(side_effect=close_store)
        runtime = AssistantRuntime(
            application=Mock(spec=AssistantApplication),
            store=store,
            qq_settings=OneBotSettings(),
            qq_connection=connection,
            qq_channel=Mock(),
        )

        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())

        self.assertEqual(events, [])
        connection.aclose.assert_not_awaited()
        store.close.assert_not_awaited()

    def test_runtime_registers_only_approved_builtin_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            runtime = AssistantRuntime(
                llm_settings=llm_settings(enabled=False),
                database_settings=DatabaseSettings(
                    data_dir=path.parent,
                    database_path=path,
                ),
            )

            self.assertEqual(
                [
                    definition.name
                    for definition in runtime.tool_registry.list()
                ],
                ["system.current_time"],
            )
            self.assertIsInstance(
                runtime.tool_service,
                ToolExecutionService,
            )
            asyncio.run(runtime.aclose())

    def test_runtime_enables_model_tools_only_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            runtime = AssistantRuntime(
                llm_settings=llm_settings(
                    enabled=True,
                    tool_calling_enabled=True,
                ),
                database_settings=DatabaseSettings(
                    data_dir=path.parent,
                    database_path=path,
                ),
            )

            self.assertIsInstance(
                runtime.model_tool_catalog,
                ModelToolCatalog,
            )
            self.assertTrue(runtime.model_tool_orchestrator.enabled)
            self.assertIs(
                runtime.application.model_orchestrator,
                runtime.model_tool_orchestrator,
            )
            self.assertEqual(
                [
                    tool.name
                    for tool in runtime.model_tool_catalog.list()
                ],
                ["system.current_time"],
            )
            asyncio.run(runtime.aclose())

    def test_runtime_keeps_tool_calling_disabled_if_either_switch_is_off(self):
        for enabled, tool_calling_enabled in (
            (True, False),
            (False, True),
            (False, False),
        ):
            with self.subTest(
                enabled=enabled,
                tool_calling_enabled=tool_calling_enabled,
            ), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "assistant.db"
                with (
                    patch("core.runtime.ModelToolCatalog") as catalog_type,
                    patch(
                        "core.runtime.ModelToolOrchestrator"
                    ) as orchestrator_type,
                ):
                    runtime = AssistantRuntime(
                        llm_settings=llm_settings(
                            enabled=enabled,
                            tool_calling_enabled=tool_calling_enabled,
                        ),
                        database_settings=DatabaseSettings(
                            data_dir=path.parent,
                            database_path=path,
                        ),
                    )

                catalog_type.assert_not_called()
                orchestrator_type.assert_not_called()
                self.assertIsNone(runtime.model_tool_catalog)
                self.assertIsNone(runtime.model_tool_orchestrator)
                self.assertIsNone(runtime.application.model_orchestrator)
                asyncio.run(runtime.aclose())

    def test_runtime_requires_at_least_one_exportable_model_tool(self):
        registries = []
        empty_registry = ToolRegistry()
        registries.append(empty_registry)

        high_risk_registry = ToolRegistry()
        high_risk_registry.register(
            runtime_tool_definition(
                "example.high-risk",
                risk=ToolRisk.HIGH,
            )
        )
        registries.append(high_risk_registry)

        invalid_registry = ToolRegistry()
        invalid_registry.register(
            runtime_tool_definition(
                "example.invalid",
                arguments_model=InvalidRuntimeToolArguments,
            )
        )
        registries.append(invalid_registry)

        for registry in registries:
            with self.subTest(tool_names=[
                definition.name for definition in registry.list()
            ]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "assistant.db"
                with patch(
                    "core.runtime.ModelToolOrchestrator"
                ) as orchestrator_type:
                    runtime = AssistantRuntime(
                        llm_settings=llm_settings(
                            enabled=True,
                            tool_calling_enabled=True,
                        ),
                        database_settings=DatabaseSettings(
                            data_dir=path.parent,
                            database_path=path,
                        ),
                        tool_registry=registry,
                    )

                self.assertIsInstance(
                    runtime.model_tool_catalog,
                    ModelToolCatalog,
                )
                orchestrator_type.assert_not_called()
                self.assertIsNone(runtime.model_tool_orchestrator)
                self.assertIsNone(runtime.application.model_orchestrator)
                asyncio.run(runtime.aclose())

    def test_runtime_enables_tools_when_catalog_has_a_valid_entry(self):
        registry = ToolRegistry()
        registry.register(
            runtime_tool_definition("example.valid")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            runtime = AssistantRuntime(
                llm_settings=llm_settings(
                    enabled=True,
                    tool_calling_enabled=True,
                ),
                database_settings=DatabaseSettings(
                    data_dir=path.parent,
                    database_path=path,
                ),
                tool_registry=registry,
            )

            self.assertEqual(
                [tool.name for tool in runtime.model_tool_catalog.list()],
                ["example.valid"],
            )
            self.assertIsNotNone(runtime.model_tool_orchestrator)
            self.assertIs(
                runtime.application.model_orchestrator,
                runtime.model_tool_orchestrator,
            )
            asyncio.run(runtime.aclose())

    def test_explicit_tool_dependencies_are_preserved_without_database(self):
        registry = ToolRegistry()
        service = Mock()
        application = Mock(spec=AssistantApplication)

        with patch("core.runtime.SqliteStore") as store_type:
            runtime = AssistantRuntime(
                application=application,
                tool_registry=registry,
                tool_service=service,
            )

        store_type.assert_not_called()
        self.assertIs(runtime.tool_registry, registry)
        self.assertIs(runtime.tool_service, service)

    def test_window_state_is_copied_at_the_runtime_boundary(self):
        runtime = AssistantRuntime(monitor=Mock(), application=Mock())
        report = {"appName": "Code", "appId": "code"}

        runtime.report_window(report)
        report["appName"] = "Mutated"

        self.assertEqual(runtime.current_window()["appName"], "Code")

    def test_scenario_check_uses_unified_application(self):
        monitor = Mock()
        monitor.get_status.return_value = {"cpu": {"percent": 5}}
        application = Mock()
        application.handle = AsyncMock()
        scenario_engine = Mock()
        scenario_engine.detect.return_value = {
            "scenarioId": "focus_mode",
            "text": "休息一下",
            "expression": "happy",
            "motion": "wave",
        }
        runtime = AssistantRuntime(
            monitor=monitor,
            application=application,
            scenario_engine=scenario_engine,
        )
        runtime.report_window({"appName": "Code"})

        asyncio.run(runtime.check_scenarios())

        application.handle.assert_awaited_once()
        message = application.handle.await_args.args[0]
        self.assertEqual(message.content.scenario_id, "focus_mode")
        self.assertEqual(message.content.text, "休息一下")

    def test_disabled_settings_build_demo_application_and_requested_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "assistant.db"
            settings = DatabaseSettings(
                data_dir=path.parent,
                database_path=path,
            )
            tts = Mock(name="side_effect_free_tts")

            with patch(
                "core.runtime.TTSService",
                return_value=tts,
            ) as tts_type:
                runtime = AssistantRuntime(
                    llm_settings=llm_settings(enabled=False),
                    database_settings=settings,
                )

            self.assertTrue(path.exists())
            tts_type.assert_called_once_with()
            self.assertIs(runtime.application.tts, tts)
            self.assertIsInstance(
                runtime.application.llm,
                DemoLanguageModelGateway,
            )
            self.assertEqual(runtime.llm_mode, "demo")
            asyncio.run(runtime.aclose())

    def test_enabled_settings_select_openai_adapter_without_network_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            tts = Mock(name="side_effect_free_tts")
            with patch(
                "core.runtime.TTSService",
                return_value=tts,
            ) as tts_type:
                runtime = AssistantRuntime(
                    llm_settings=llm_settings(enabled=True),
                    database_settings=DatabaseSettings(
                        data_dir=path.parent,
                        database_path=path,
                    ),
                )

            tts_type.assert_called_once_with()
            self.assertIs(runtime.application.tts, tts)
            self.assertIsInstance(
                runtime.application.llm,
                OpenAICompatibleGateway,
            )
            self.assertEqual(runtime.llm_mode, "configured")
            asyncio.run(runtime.aclose())

    def test_explicit_application_never_constructs_a_database(self):
        application = Mock(spec=AssistantApplication)

        with patch("core.runtime.SqliteStore") as store_type:
            runtime = AssistantRuntime(application=application)

        store_type.assert_not_called()
        self.assertIs(runtime.application, application)
        self.assertIsNone(runtime.store)

    def test_close_is_idempotent_and_leaves_external_store_open(self):
        store = Mock()
        store.close = AsyncMock()
        application = Mock(spec=AssistantApplication)

        runtime = AssistantRuntime(application=application, store=store)
        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())

        store.close.assert_not_awaited()

    def test_close_retries_only_failed_owned_resource_and_keeps_first_error(self):
        store = Mock()
        store.close = AsyncMock()
        gateway = Mock(spec=["close"])
        gateway.close = AsyncMock()
        tts = Mock(spec=["close"])
        tts.close = AsyncMock()
        connection = Mock()
        connection.aclose = AsyncMock(
            side_effect=[RuntimeError("temporary close failure"), None]
        )

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=gateway),
            patch("core.runtime.TTSService", return_value=tts),
            patch(
                "core.runtime.OneBotConnectionManager",
                return_value=connection,
            ),
            patch("core.runtime.OneBotChannel", return_value=Mock()),
        ):
            runtime = AssistantRuntime(
                llm_settings=llm_settings(enabled=True),
                database_settings=DatabaseSettings(
                    data_dir=Path("/tmp"),
                    database_path=Path("/tmp/assistant.db"),
                ),
            )

        with self.assertRaisesRegex(RuntimeError, "temporary close failure"):
            asyncio.run(runtime.aclose())

        self.assertFalse(runtime._closed)
        gateway.close.assert_awaited_once()
        tts.close.assert_awaited_once()
        store.close.assert_awaited_once()
        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())
        self.assertTrue(runtime._closed)
        self.assertEqual(connection.aclose.await_count, 2)
        gateway.close.assert_awaited_once()
        tts.close.assert_awaited_once()
        store.close.assert_awaited_once()

    def test_normal_close_releases_every_owned_resource_once(self):
        store = Mock()
        store.close = AsyncMock()
        gateway = Mock(spec=["close"])
        gateway.close = Mock()
        tts = Mock(spec=["aclose"])
        tts.aclose = AsyncMock()
        connection = Mock()
        connection.aclose = AsyncMock()
        channel = Mock(spec=["close"])
        channel.close = Mock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=gateway),
            patch("core.runtime.TTSService", return_value=tts),
            patch(
                "core.runtime.OneBotConnectionManager",
                return_value=connection,
            ),
            patch("core.runtime.OneBotChannel", return_value=channel),
        ):
            runtime = AssistantRuntime(
                llm_settings=llm_settings(enabled=True),
                database_settings=DatabaseSettings(
                    data_dir=Path("/tmp"),
                    database_path=Path("/tmp/assistant.db"),
                ),
            )

        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())

        channel.close.assert_called_once_with()
        connection.aclose.assert_awaited_once()
        tts.aclose.assert_awaited_once()
        gateway.close.assert_called_once_with()
        store.close.assert_awaited_once()
        self.assertTrue(runtime._closed)

    def test_late_channel_failure_rolls_back_all_owned_resources(self):
        store = Mock()
        store._close_sync = Mock()
        gateway = Mock(spec=["close"])
        gateway.close = Mock()
        tts = Mock(spec=["close"])
        tts.close = Mock()
        connection = Mock()
        connection.aclose = AsyncMock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=gateway),
            patch("core.runtime.TTSService", return_value=tts),
            patch(
                "core.runtime.OneBotConnectionManager",
                return_value=connection,
            ),
            patch(
                "core.runtime.OneBotChannel",
                side_effect=RuntimeError("late channel setup failed"),
            ),
        ):
            async def construct_runtime():
                AssistantRuntime(
                    llm_settings=llm_settings(enabled=True),
                    database_settings=DatabaseSettings(
                        data_dir=Path("/tmp"),
                        database_path=Path("/tmp/assistant.db"),
                    ),
                )

            with self.assertRaisesRegex(
                RuntimeError,
                "late channel setup failed",
            ):
                asyncio.run(construct_runtime())

        connection.aclose.assert_awaited_once()
        tts.close.assert_called_once_with()
        gateway.close.assert_called_once_with()
        store._close_sync.assert_called_once_with()

    def test_runtime_builds_model_tool_dependencies_in_required_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            events: list[str] = []
            store = Mock()
            registry = Mock()
            service = Mock()
            catalog = Mock()
            gateway = Mock()
            orchestrator = Mock()

            with (
                patch(
                    "core.runtime.SqliteStore",
                    side_effect=lambda _: (events.append("store"), store)[1],
                ),
                patch(
                    "core.runtime.build_builtin_registry",
                    side_effect=lambda: (
                        events.append("registry"),
                        registry,
                    )[1],
                ),
                patch(
                    "core.runtime.ToolExecutionService",
                    side_effect=lambda **_: (
                        events.append("service"),
                        service,
                    )[1],
                ),
                patch(
                    "core.runtime.ModelToolCatalog",
                    side_effect=lambda _: (
                        events.append("catalog"),
                        catalog,
                    )[1],
                ),
                patch(
                    "core.runtime.OpenAICompatibleGateway",
                    side_effect=lambda _: (
                        events.append("gateway"),
                        gateway,
                    )[1],
                ),
                patch(
                    "core.runtime.ModelToolOrchestrator",
                    side_effect=lambda **_: (
                        events.append("orchestrator"),
                        orchestrator,
                    )[1],
                ),
                patch(
                    "core.runtime.TTSService",
                    return_value=Mock(),
                ),
                patch(
                    "core.runtime.AssistantApplication",
                    return_value=Mock(),
                ),
            ):
                runtime = AssistantRuntime(
                    llm_settings=llm_settings(
                        enabled=True,
                        tool_calling_enabled=True,
                    ),
                    database_settings=DatabaseSettings(
                        data_dir=path.parent,
                        database_path=path,
                    ),
                    qq_settings=OneBotSettings(),
                    qq_connection=Mock(),
                    qq_channel=Mock(),
                )

            self.assertEqual(
                events,
                [
                    "store",
                    "registry",
                    "service",
                    "catalog",
                    "gateway",
                    "orchestrator",
                ],
            )
            self.assertIs(runtime.model_tool_catalog, catalog)
            self.assertIs(runtime.model_tool_orchestrator, orchestrator)

    def test_runtime_closes_owned_store_when_gateway_construction_fails(self):
        store = Mock()
        store._close_sync = Mock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch(
                "core.runtime.OpenAICompatibleGateway",
                side_effect=RuntimeError("gateway setup failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "gateway setup failed"):
                AssistantRuntime(
                    llm_settings=llm_settings(enabled=True),
                    database_settings=DatabaseSettings(
                        data_dir=Path("/tmp"),
                        database_path=Path("/tmp/assistant.db"),
                    ),
                )

        store._close_sync.assert_called_once_with()

    def test_runtime_closes_owned_store_when_tts_construction_fails(self):
        store = Mock()
        store._close_sync = Mock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=Mock()),
            patch(
                "core.runtime.TTSService",
                side_effect=RuntimeError("tts setup failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "tts setup failed"):
                AssistantRuntime(
                    llm_settings=llm_settings(enabled=True),
                    database_settings=DatabaseSettings(
                        data_dir=Path("/tmp"),
                        database_path=Path("/tmp/assistant.db"),
                    ),
                )

        store._close_sync.assert_called_once_with()

    def test_runtime_closes_gateway_when_orchestrator_construction_fails(self):
        store = Mock()
        store._close_sync = Mock()
        gateway = Mock(spec=["close"])
        gateway.close = Mock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=gateway),
            patch(
                "core.runtime.ModelToolOrchestrator",
                side_effect=RuntimeError("orchestrator setup failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "orchestrator setup failed",
            ):
                AssistantRuntime(
                    llm_settings=llm_settings(
                        enabled=True,
                        tool_calling_enabled=True,
                    ),
                    database_settings=DatabaseSettings(
                        data_dir=Path("/tmp"),
                        database_path=Path("/tmp/assistant.db"),
                    ),
                )

        gateway.close.assert_called_once_with()
        store._close_sync.assert_called_once_with()

    def test_runtime_closes_tts_gateway_and_store_if_application_fails(self):
        store = Mock()
        store._close_sync = Mock()
        gateway = Mock(spec=["close"])
        gateway.close = Mock()
        tts = Mock(spec=["close"])
        tts.close = Mock()

        with (
            patch("core.runtime.SqliteStore", return_value=store),
            patch("core.runtime.OpenAICompatibleGateway", return_value=gateway),
            patch("core.runtime.TTSService", return_value=tts),
            patch(
                "core.runtime.AssistantApplication",
                side_effect=RuntimeError("application setup failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "application setup failed",
            ):
                AssistantRuntime(
                    llm_settings=llm_settings(enabled=True),
                    database_settings=DatabaseSettings(
                        data_dir=Path("/tmp"),
                        database_path=Path("/tmp/assistant.db"),
                    ),
                )

        tts.close.assert_called_once_with()
        gateway.close.assert_called_once_with()
        store._close_sync.assert_called_once_with()

    def test_status_only_exposes_llm_mode_from_runtime_configuration(self):
        monitor = Mock()
        monitor.get_status.return_value = {
            "cpu": {
                "percent": 5,
                "cores": 8,
                "privatePath": "/private/cpu",
            },
            "memory": {
                "total": "16 GB",
                "used": "8 GB",
                "percent": 50,
                "api_key": "nested-key",
            },
            "uptime": "1 day",
            "assistant": {
                "healthy": True,
                "base_url": "https://private.example",
            },
            "privatePath": "/private/assistant.db",
            "base_url": "https://private.example",
            "api_key": "private-api-key",
            "data_dir": "/private/data",
        }
        application = Mock(spec=AssistantApplication)
        application.llm = DemoLanguageModelGateway()
        runtime = AssistantRuntime(
            monitor=monitor,
            application=application,
            llm_settings=llm_settings(enabled=True),
        )

        status = runtime.status()

        self.assertEqual(
            status,
            {
                "cpu": {"percent": 5, "cores": 8},
                "memory": {
                    "total": "16 GB",
                    "used": "8 GB",
                    "percent": 50,
                },
                "uptime": "1 day",
                "assistant": {"llmMode": "configured"},
            },
        )
        serialized = json.dumps(status)
        for secret in (
            "privatePath",
            "base_url",
            "api_key",
            "data_dir",
            "private-api-key",
            "nested-key",
            "/private",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)

    def test_imported_app_factory_does_not_construct_runtime_or_database(self):
        with (
            patch("api.app.AssistantRuntime") as runtime_type,
            patch("core.runtime.SqliteStore") as store_type,
        ):
            application = create_app()

        runtime_type.assert_not_called()
        store_type.assert_not_called()
        self.assertIsNone(application.state.runtime)

    def test_fresh_import_does_not_instantiate_tts_or_database(self):
        backend = Path(__file__).resolve().parents[1]
        probe = """
import core.tts as core_tts
import infrastructure.sqlite_store as sqlite_store

class ExplodingDependency:
    def __init__(self, *args, **kwargs):
        raise AssertionError("dependency was instantiated during import")

core_tts.TTSService = ExplodingDependency
sqlite_store.SqliteStore = ExplodingDependency

import api.app

application = api.app.create_app()
assert application.state.runtime is None
"""

        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=backend,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )

    def test_lifespan_closes_runtime_when_subscription_fails(self):
        runtime = Mock()
        runtime.application.publisher.subscribe.side_effect = RuntimeError(
            "subscription failed"
        )
        runtime.aclose = AsyncMock()
        application = create_app(runtime_instance=runtime)

        async def exercise():
            with self.assertRaisesRegex(RuntimeError, "subscription failed"):
                async with lifespan(application):
                    pass

        asyncio.run(exercise())

        runtime.aclose.assert_awaited_once()

    def test_foreground_monitor_reports_only_changes(self):
        reports = []
        get_app = Mock(return_value={"appName": "Code", "appId": "code"})
        monitor = ForegroundWindowMonitor(get_app, reports.append)

        asyncio.run(monitor.poll_once())
        asyncio.run(monitor.poll_once())

        self.assertEqual(reports, [{"appName": "Code", "appId": "code"}])
        self.assertEqual(get_app.call_count, 2)
