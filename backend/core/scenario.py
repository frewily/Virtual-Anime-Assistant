import random
import time
from datetime import datetime

from core.config_loader import get_scenarios


class ScenarioEngine:
    def __init__(self, *, scenarios=None, clock=None, now_provider=None):
        self.scenarios = scenarios if scenarios is not None else get_scenarios()
        self._clock = clock or time.time
        self._now = now_provider or datetime.now
        self._cooldowns: dict[str, float] = {}
        self._condition_started: dict[str, float] = {}
        self._active_app: str | None = None
        self._active_since: float | None = None

    def detect(self, system_status: dict, window: dict | None = None) -> dict | None:
        now = self._now()
        self._update_active_app(window)

        matches = [
            scenario
            for scenario in self.scenarios
            if self._matches_scenario(scenario, system_status, window, now)
        ]
        matches.sort(key=lambda scenario: scenario.get("priority", 0), reverse=True)

        selected = next(
            (
                scenario
                for scenario in matches
                if not self._in_cooldown(
                    scenario["id"], scenario.get("cooldownSeconds", 60)
                )
            ),
            None,
        )
        if selected is None:
            return None

        self._cooldowns[selected["id"]] = self._clock()
        response = selected["response"]
        return {
            "scenarioId": selected["id"],
            "text": random.choice(response["templates"]).format(
                appName=(window or {}).get("appName", "当前应用")
            ),
            "expression": response["expression"],
            "motion": response["motion"],
        }

    def _matches_scenario(
        self,
        scenario: dict,
        status: dict,
        window: dict | None,
        now: datetime,
    ) -> bool:
        trigger = scenario.get("trigger", {})
        trigger_type = trigger.get("type")

        if trigger_type == "cpu_threshold":
            cpu = status.get("cpu", {}).get("percent", 0)
            matches = cpu >= trigger.get("threshold", 100)
            return self._meets_duration(
                scenario["id"], matches, trigger.get("duration", 0)
            )

        if trigger_type == "time_range":
            return _in_time_range(now, trigger)

        if trigger_type == "app_detect":
            app_name = (window or {}).get("appName", "")
            matches_app = any(target in app_name for target in trigger.get("apps", []))
            return matches_app and _in_time_range(now, trigger.get("timeRange", {}))

        if trigger_type == "app_duration":
            app_name = (window or {}).get("appName", "")
            matches_app = any(target in app_name for target in trigger.get("apps", []))
            duration_seconds = trigger.get("duration", 0) * 60
            return (
                matches_app
                and self._active_since is not None
                and self._clock() - self._active_since >= duration_seconds
            )

        return False

    def _meets_duration(self, scenario_id: str, matches: bool, seconds: float) -> bool:
        if not matches:
            self._condition_started.pop(scenario_id, None)
            return False
        if seconds <= 0:
            return True
        started = self._condition_started.setdefault(scenario_id, self._clock())
        return self._clock() - started >= seconds

    def _in_cooldown(self, scenario_id: str, seconds: float) -> bool:
        last = self._cooldowns.get(scenario_id)
        return last is not None and self._clock() - last < seconds

    def _update_active_app(self, window: dict | None) -> None:
        app_name = (window or {}).get("appName")
        if app_name and app_name != self._active_app:
            self._active_app = app_name
            self._active_since = self._clock()


def _minutes_since_midnight(value: str) -> int:
    hours, minutes = (int(part) for part in value.split(":", maxsplit=1))
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise ValueError(f"invalid time value: {value}")
    return hours * 60 + minutes


def _in_time_range(now: datetime, time_range: dict) -> bool:
    if not time_range:
        return True
    start = _minutes_since_midnight(time_range["start"])
    end = _minutes_since_midnight(time_range["end"])
    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end
