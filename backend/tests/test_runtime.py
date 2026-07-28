import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.monitor import ForegroundWindowMonitor
from api.app import create_app, lifespan
from application.assistant import AssistantApplication
from core.runtime import AssistantRuntime
from infrastructure.database_config import DatabaseSettings
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway
from llm.openai_compatible import OpenAICompatibleGateway
from tools.registry import ToolRegistry
from tools.service import ToolExecutionService


def llm_settings(*, enabled: bool) -> LLMSettings:
    return LLMSettings(
        enabled=enabled,
        base_url="https://llm.example/v1" if enabled else None,
        api_key="private-api-key" if enabled else None,
        model="configured-model" if enabled else None,
        timeout_seconds=10,
        max_context_messages=8,
        max_context_chars=5000,
    )


class RuntimeTests(unittest.TestCase):
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

    def test_close_is_idempotent_and_closes_the_owned_store(self):
        store = Mock()
        store.close = AsyncMock()
        application = Mock(spec=AssistantApplication)

        runtime = AssistantRuntime(application=application, store=store)
        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())

        store.close.assert_awaited_once()

    def test_close_retries_after_the_store_close_fails(self):
        store = Mock()
        store.close = AsyncMock(
            side_effect=[RuntimeError("temporary close failure"), None]
        )
        runtime = AssistantRuntime(
            application=Mock(spec=AssistantApplication),
            store=store,
        )

        with self.assertRaisesRegex(RuntimeError, "temporary close failure"):
            asyncio.run(runtime.aclose())

        self.assertFalse(runtime._closed)
        asyncio.run(runtime.aclose())
        asyncio.run(runtime.aclose())
        self.assertTrue(runtime._closed)
        self.assertEqual(store.close.await_count, 2)

    def test_runtime_builds_failure_prone_components_before_opening_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            with (
                patch(
                    "core.runtime.TTSService",
                    side_effect=RuntimeError("tts setup failed"),
                ),
                patch("core.runtime.SqliteStore") as store_type,
            ):
                with self.assertRaisesRegex(RuntimeError, "tts setup failed"):
                    AssistantRuntime(
                        llm_settings=llm_settings(enabled=False),
                        database_settings=DatabaseSettings(
                            data_dir=path.parent,
                            database_path=path,
                        ),
                    )

        store_type.assert_not_called()

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
