import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.messages import MessageSource
from domain.tools import ToolRequest, ToolRequestState, ToolRisk, ToolSource
from infrastructure.sqlite_store import SqliteStore
from tools.builtin import (
    CurrentTimeArguments,
    InvalidTimezoneError,
    build_builtin_registry,
    current_time,
)
from tools.catalog import ModelToolCallContext
from tools.service import ToolExecutionService


class BuiltinToolTests(unittest.TestCase):
    def test_current_time_accepts_iana_zone_and_defaults_to_local_zone(self):
        utc = asyncio.run(
            current_time(CurrentTimeArguments(timezone="UTC"))
        )
        local = asyncio.run(
            current_time(CurrentTimeArguments())
        )

        self.assertEqual(utc["timezone"], "UTC")
        self.assertTrue(utc["iso"].endswith("+00:00"))
        self.assertTrue(local["timezone"])
        self.assertIn("T", local["iso"])

    def test_invalid_timezone_has_stable_error_code(self):
        with self.assertRaises(InvalidTimezoneError) as raised:
            asyncio.run(
                current_time(
                    CurrentTimeArguments(timezone="Mars/Olympus_Mons")
                )
            )

        self.assertEqual(raised.exception.error_code, "invalid_timezone")
        self.assertNotIn(
            "Mars/Olympus_Mons",
            str(raised.exception),
        )

    def test_production_registry_contains_only_approved_read_only_tool(self):
        registry = build_builtin_registry()

        definitions = registry.list()

        self.assertEqual(
            [definition.name for definition in definitions],
            ["system.current_time"],
        )
        self.assertEqual(definitions[0].risk, ToolRisk.LOW)
        self.assertEqual(definitions[0].timeout_seconds, 2)
        self.assertEqual(
            definitions[0].allowed_sources,
            frozenset(
                {
                    ToolSource.DESKTOP,
                    ToolSource.MODEL,
                    ToolSource.SYSTEM,
                }
            ),
        )

    def test_time_tool_executes_without_confirmation_and_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore(Path(directory) / "assistant.db")
            service = ToolExecutionService(
                registry=build_builtin_registry(),
                repository=store,
            )

            result = asyncio.run(
                service.request(
                    ToolRequest(
                        correlation_id="message-1",
                        source=ToolSource.DESKTOP,
                        tool_name="system.current_time",
                        arguments={"timezone": "UTC"},
                    )
                )
            )

            self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
            self.assertIsNone(result.confirmation)
            self.assertEqual(result.result["timezone"], "UTC")
            asyncio.run(store.close())

    def test_invalid_timezone_is_normalized_by_execution_service(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore(Path(directory) / "assistant.db")
            service = ToolExecutionService(
                registry=build_builtin_registry(),
                repository=store,
            )

            result = asyncio.run(
                service.request(
                    ToolRequest(
                        correlation_id="message-1",
                        source=ToolSource.DESKTOP,
                        tool_name="system.current_time",
                        arguments={"timezone": "Mars/Olympus_Mons"},
                    )
                )
            )

            self.assertEqual(result.state, ToolRequestState.FAILED)
            self.assertEqual(result.error_code, "invalid_timezone")
            asyncio.run(store.close())

    def test_time_tool_accepts_model_source_without_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore(Path(directory) / "assistant.db")
            service = ToolExecutionService(
                registry=build_builtin_registry(),
                repository=store,
                runtime_profile="desktop",
            )

            result = asyncio.run(
                service.request(
                    ToolRequest(
                        correlation_id="model-message-1",
                        source=ToolSource.MODEL,
                        tool_name="system.current_time",
                        arguments={"timezone": " UTC "},
                    ),
                    model_context=ModelToolCallContext(
                        channel=MessageSource.DESKTOP,
                        advertised_tool_names=frozenset(
                            {"system.current_time"}
                        ),
                    ),
                )
            )
            stored = asyncio.run(store.get_request(result.request_id))

            self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
            self.assertIsNone(result.confirmation)
            self.assertEqual(result.result["timezone"], "UTC")
            self.assertEqual(stored.source, ToolSource.MODEL)
            asyncio.run(store.close())


if __name__ == "__main__":
    unittest.main()
