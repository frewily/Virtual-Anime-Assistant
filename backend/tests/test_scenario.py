import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scenario import ScenarioEngine, _in_time_range


def scenario(scenario_id, trigger, *, priority=0, cooldown=60):
    return {
        "id": scenario_id,
        "priority": priority,
        "cooldownSeconds": cooldown,
        "trigger": trigger,
        "response": {
            "expression": "happy",
            "motion": "wave",
            "templates": [scenario_id],
        },
    }


class ScenarioEngineTests(unittest.TestCase):
    def test_highest_priority_match_wins(self):
        scenarios = [
            scenario("low", {"type": "cpu_threshold", "threshold": 10}, priority=10),
            scenario("high", {"type": "cpu_threshold", "threshold": 10}, priority=90),
        ]
        engine = ScenarioEngine(scenarios=scenarios)

        result = engine.detect({"cpu": {"percent": 50}})

        self.assertEqual(result["text"], "high")

    def test_cpu_threshold_must_last_for_configured_seconds(self):
        current_time = [100.0]
        engine = ScenarioEngine(
            scenarios=[
                scenario(
                    "cpu",
                    {"type": "cpu_threshold", "threshold": 80, "duration": 30},
                )
            ],
            clock=lambda: current_time[0],
        )

        self.assertIsNone(engine.detect({"cpu": {"percent": 90}}))
        current_time[0] = 129.0
        self.assertIsNone(engine.detect({"cpu": {"percent": 90}}))
        current_time[0] = 130.0
        self.assertIsNotNone(engine.detect({"cpu": {"percent": 90}}))

    def test_cooldown_comes_from_scenario_configuration(self):
        current_time = [100.0]
        engine = ScenarioEngine(
            scenarios=[
                scenario(
                    "cpu",
                    {"type": "cpu_threshold", "threshold": 10},
                    cooldown=120,
                )
            ],
            clock=lambda: current_time[0],
        )

        self.assertIsNotNone(engine.detect({"cpu": {"percent": 90}}))
        current_time[0] = 219.0
        self.assertIsNone(engine.detect({"cpu": {"percent": 90}}))
        current_time[0] = 220.0
        self.assertIsNotNone(engine.detect({"cpu": {"percent": 90}}))

    def test_time_range_uses_minutes_and_crosses_midnight(self):
        time_range = {"start": "23:45", "end": "06:15"}

        self.assertFalse(_in_time_range(datetime(2026, 1, 1, 23, 30), time_range))
        self.assertTrue(_in_time_range(datetime(2026, 1, 1, 23, 50), time_range))
        self.assertTrue(_in_time_range(datetime(2026, 1, 2, 6, 10), time_range))
        self.assertFalse(_in_time_range(datetime(2026, 1, 2, 6, 20), time_range))
