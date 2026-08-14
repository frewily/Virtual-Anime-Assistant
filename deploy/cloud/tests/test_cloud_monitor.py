import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/cloud/scripts/cloud_monitor.py"
WRAPPER = ROOT / "deploy/cloud/scripts/cloud-monitor.sh"
SPEC = importlib.util.spec_from_file_location("cloud_monitor", SCRIPT)
cloud_monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud_monitor)

NOW = 1_776_163_200.0
BACKUP_AT = cloud_monitor.utc_text(NOW - 60)


def base_state(**updates):
    state = cloud_monitor.initial_state(NOW)
    state.update({
        "overallState": "degraded",
        "vaaState": "ready",
        "onebotState": "disconnected",
        "backupState": "fresh",
        "latestBackupAt": BACKUP_AT,
    })
    state.update(updates)
    return state


def observation(onebot="disconnected", vaa="ready", backup="fresh"):
    return cloud_monitor.Observation(vaa, onebot, backup, BACKUP_AT)


class Config(object):
    def __init__(self, directory):
        self.compose_file = str(Path(directory) / "docker-compose.yml")
        self.public_state_file = str(Path(directory) / "public.json")
        self.private_state_file = str(Path(directory) / "private.json")
        self.backup_directory = str(Path(directory) / "backups")
        self.base_url = "http://127.0.0.1:8080"
        self.monitor_lock_file = str(Path(directory) / "monitor.lock")
        self.deploy_lock_file = str(Path(directory) / "deploy.lock")


class FakeRunner(object):
    def __init__(self, observations, recheck="disconnected", deploy_busy=False):
        self.observations = list(observations)
        self.recheck = recheck
        self.deploy_busy = deploy_busy
        self.restart_called = False

    @contextlib.contextmanager
    def acquire_monitor_lock(self, _path):
        yield

    def observe(self, _config, _now):
        return self.observations.pop(0)

    def observe_onebot(self, _config):
        return self.recheck

    def deployment_lock_busy(self, _path):
        return self.deploy_busy

    def restart_napcat(self, _compose_file):
        self.restart_called = True


class CloudMonitorTests(unittest.TestCase):
    def test_third_consecutive_disconnect_requests_one_recovery(self):
        result = cloud_monitor.evaluate(
            base_state(consecutiveOnebotFailures=2),
            observation(),
            NOW,
        )

        self.assertTrue(result.restart_napcat)
        self.assertEqual(result.state["consecutiveOnebotFailures"], 3)

    def test_recovery_limit_enters_alert_without_restart(self):
        result = cloud_monitor.evaluate(
            base_state(
                consecutiveOnebotFailures=2,
                recoveryTimestamps=[NOW - 590, NOW - 300],
            ),
            observation(),
            NOW,
        )

        self.assertFalse(result.restart_napcat)
        self.assertEqual(result.state["alertCode"], "recovery_exhausted")
        self.assertEqual(result.state["recoveriesInWindow"], 2)

    def test_connected_disabled_misconfigured_and_unavailable_are_bounded(self):
        cases = (
            (observation("connected"), "healthy", None, 0),
            (observation("disabled"), "degraded", None, 0),
            (
                observation("misconfigured"),
                "alerting",
                "configuration_required",
                0,
            ),
            (
                observation("unknown", vaa="unavailable"),
                "alerting",
                "vaa_unavailable",
                2,
            ),
        )
        for seen, overall, alert, failures in cases:
            with self.subTest(onebot=seen.onebot_state, vaa=seen.vaa_state):
                result = cloud_monitor.evaluate(
                    base_state(consecutiveOnebotFailures=2), seen, NOW
                )
                self.assertFalse(result.restart_napcat)
                self.assertEqual(result.state["overallState"], overall)
                self.assertEqual(result.state["alertCode"], alert)
                self.assertEqual(
                    result.state["consecutiveOnebotFailures"], failures
                )

    def test_stale_backup_degrades_healthy_connection(self):
        result = cloud_monitor.evaluate(
            base_state(), observation("connected", backup="stale"), NOW
        )

        self.assertEqual(result.state["overallState"], "degraded")
        self.assertEqual(result.state["alertCode"], "backup_stale")

    def test_unknown_onebot_state_never_triggers_recovery(self):
        result = cloud_monitor.evaluate(
            base_state(consecutiveOnebotFailures=2),
            observation("unknown"),
            NOW,
        )

        self.assertFalse(result.restart_napcat)
        self.assertEqual(result.state["overallState"], "degraded")
        self.assertEqual(result.state["consecutiveOnebotFailures"], 2)

    def test_restart_is_skipped_when_recheck_has_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(directory)
            Path(config.private_state_file).write_text(
                json.dumps(base_state(consecutiveOnebotFailures=2))
            )
            runner = FakeRunner([observation()], recheck="connected")

            cloud_monitor.run_once(config, runner=runner, now=lambda: NOW)

            self.assertFalse(runner.restart_called)
            state = json.loads(Path(config.public_state_file).read_text())
            self.assertEqual(state["onebotState"], "connected")

    def test_restart_records_recovery_without_sensitive_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(directory)
            Path(config.private_state_file).write_text(
                json.dumps(base_state(consecutiveOnebotFailures=2))
            )
            runner = FakeRunner([observation()])

            cloud_monitor.run_once(config, runner=runner, now=lambda: NOW)

            self.assertTrue(runner.restart_called)
            payload = Path(config.public_state_file).read_text()
            state = json.loads(payload)
            self.assertEqual(state["recoveriesInWindow"], 1)
            self.assertEqual(state["consecutiveOnebotFailures"], 0)
            self.assertEqual(os.stat(config.public_state_file).st_mode & 0o777, 0o640)
            for forbidden in (
                "token",
                "apiKey",
                "allowedUserIds",
                "allowedGroupIds",
                "2994508531",
                "601888065",
                "recoveryTimestamps",
            ):
                self.assertNotIn(forbidden, payload)

    def test_locked_deployment_records_state_without_docker_action(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(directory)
            Path(config.private_state_file).write_text(
                json.dumps(base_state(consecutiveOnebotFailures=2))
            )
            runner = FakeRunner([observation()], deploy_busy=True)

            cloud_monitor.run_once(config, runner=runner, now=lambda: NOW)

            state = json.loads(Path(config.public_state_file).read_text())
            self.assertFalse(runner.restart_called)
            self.assertEqual(state["alertCode"], "deployment_in_progress")

    def test_cli_help_and_wrapper_are_safe(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn(b"--compose-file", completed.stdout)
        wrapper = WRAPPER.read_text()
        implementation = SCRIPT.read_text()
        self.assertIn('exec python3 "$script_dir/cloud_monitor.py"', wrapper)
        for forbidden in (
            "shell=True",
            "secrets.env",
            "docker compose config",
            "docker compose down",
            "printenv",
        ):
            self.assertNotIn(forbidden, wrapper + implementation)


if __name__ == "__main__":
    unittest.main()
