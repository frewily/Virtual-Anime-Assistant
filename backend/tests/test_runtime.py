import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.monitor import ForegroundWindowMonitor
from core.runtime import AssistantRuntime


class RuntimeTests(unittest.TestCase):
    def test_window_state_is_copied_at_the_runtime_boundary(self):
        runtime = AssistantRuntime(monitor=Mock(), router=Mock())
        report = {"appName": "Code", "appId": "code"}

        runtime.report_window(report)
        report["appName"] = "Mutated"

        self.assertEqual(runtime.current_window()["appName"], "Code")

    def test_scenario_check_uses_current_status_and_window(self):
        monitor = Mock()
        monitor.get_status.return_value = {"cpu": {"percent": 5}}
        router = Mock()
        router.handle_scenario_check = AsyncMock()
        runtime = AssistantRuntime(monitor=monitor, router=router)
        runtime.report_window({"appName": "Code"})

        asyncio.run(runtime.check_scenarios())

        router.handle_scenario_check.assert_awaited_once_with(
            {"cpu": {"percent": 5}}, {"appName": "Code"}
        )

    def test_foreground_monitor_reports_only_changes(self):
        reports = []
        get_app = Mock(return_value={"appName": "Code", "appId": "code"})
        monitor = ForegroundWindowMonitor(get_app, reports.append)

        asyncio.run(monitor.poll_once())
        asyncio.run(monitor.poll_once())

        self.assertEqual(reports, [{"appName": "Code", "appId": "code"}])
        self.assertEqual(get_app.call_count, 2)
