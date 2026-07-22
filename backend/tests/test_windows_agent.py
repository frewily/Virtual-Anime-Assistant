import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.windows import get_foreground_app


class FakeUser32:
    def __init__(self, hwnd=100):
        self.hwnd = hwnd

    def GetForegroundWindow(self):
        return self.hwnd

    def GetWindowTextLengthW(self, _):
        return len("Project - Code")

    def GetWindowTextW(self, _, buffer, __):
        buffer.value = "Project - Code"
        return len(buffer.value)

    def GetWindowThreadProcessId(self, _, process_id_pointer):
        process_id_pointer._obj.value = 321
        return 1


class FakeProcess:
    def __init__(self, process_id):
        self.process_id = process_id

    def name(self):
        return "Code.exe"


class WindowsAgentTests(unittest.TestCase):
    def test_foreground_application_contains_process_and_window_title(self):
        result = get_foreground_app(FakeUser32(), FakeProcess)

        self.assertEqual(
            result,
            {
                "appName": "Code.exe",
                "appId": "Code.exe",
                "windowTitle": "Project - Code",
                "processId": 321,
            },
        )

    def test_missing_foreground_window_returns_none(self):
        self.assertIsNone(get_foreground_app(FakeUser32(hwnd=0), FakeProcess))
