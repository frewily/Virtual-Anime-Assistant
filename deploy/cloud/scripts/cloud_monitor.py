#!/usr/bin/env python3
"""Bounded host-side monitoring for the single-node cloud deployment.

This module intentionally stays compatible with Python 3.6 and uses only the
standard library because Alibaba Cloud Linux 3 ships an older host Python.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


Observation = collections.namedtuple(
    "Observation", "vaa_state onebot_state backup_state latest_backup_at"
)
Evaluation = collections.namedtuple("Evaluation", "state restart_napcat")

FAILURES_BEFORE_RECOVERY = 3
RECOVERY_WINDOW_SECONDS = 10 * 60
MAX_RECOVERIES_IN_WINDOW = 2
BACKUP_STALE_SECONDS = 36 * 60 * 60

_ONEBOT_STATES = {
    "connected",
    "disconnected",
    "disabled",
    "misconfigured",
}


def utc_text(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def initial_state(now):
    return {
        "schemaVersion": 1,
        "checkedAt": utc_text(now),
        "overallState": "unknown",
        "vaaState": "unknown",
        "onebotState": "unknown",
        "backupState": "unknown",
        "latestBackupAt": None,
        "consecutiveOnebotFailures": 0,
        "recoveriesInWindow": 0,
        "lastRecoveryAt": None,
        "alertCode": None,
        "recoveryTimestamps": [],
    }


def _safe_previous(previous, now):
    if not isinstance(previous, dict) or previous.get("schemaVersion") != 1:
        return initial_state(now)
    state = initial_state(now)
    failures = previous.get("consecutiveOnebotFailures", 0)
    timestamps = previous.get("recoveryTimestamps", [])
    state["consecutiveOnebotFailures"] = (
        failures if isinstance(failures, int) and 0 <= failures <= 1000 else 0
    )
    state["recoveryTimestamps"] = [
        value
        for value in timestamps
        if isinstance(value, (int, float)) and 0 <= value <= now
    ] if isinstance(timestamps, list) else []
    last_recovery = previous.get("lastRecoveryAt")
    state["lastRecoveryAt"] = (
        last_recovery if isinstance(last_recovery, str) else None
    )
    return state


def _prune_recoveries(state, now):
    minimum = now - RECOVERY_WINDOW_SECONDS
    state["recoveryTimestamps"] = [
        value for value in state["recoveryTimestamps"] if value >= minimum
    ]
    state["recoveriesInWindow"] = len(state["recoveryTimestamps"])


def evaluate(previous, observation, now):
    state = _safe_previous(previous, now)
    _prune_recoveries(state, now)
    state.update({
        "checkedAt": utc_text(now),
        "vaaState": observation.vaa_state,
        "onebotState": observation.onebot_state,
        "backupState": observation.backup_state,
        "latestBackupAt": observation.latest_backup_at,
        "alertCode": None,
    })
    restart_napcat = False

    if observation.vaa_state != "ready":
        state["overallState"] = "alerting"
        state["alertCode"] = "vaa_unavailable"
        return Evaluation(state, False)

    if observation.onebot_state == "misconfigured":
        state["overallState"] = "alerting"
        state["alertCode"] = "configuration_required"
        state["consecutiveOnebotFailures"] = 0
    elif observation.onebot_state == "disabled":
        state["overallState"] = "degraded"
        state["consecutiveOnebotFailures"] = 0
    elif observation.onebot_state == "connected":
        state["overallState"] = "healthy"
        state["consecutiveOnebotFailures"] = 0
    elif observation.onebot_state == "disconnected":
        state["overallState"] = "degraded"
        state["consecutiveOnebotFailures"] += 1
        if state["consecutiveOnebotFailures"] >= FAILURES_BEFORE_RECOVERY:
            if state["recoveriesInWindow"] >= MAX_RECOVERIES_IN_WINDOW:
                state["overallState"] = "alerting"
                state["alertCode"] = "recovery_exhausted"
            else:
                restart_napcat = True
    else:
        state["overallState"] = "degraded"

    if observation.backup_state in ("stale", "missing"):
        if state["alertCode"] is None:
            state["alertCode"] = "backup_stale"
        if state["overallState"] == "healthy":
            state["overallState"] = "degraded"

    return Evaluation(state, restart_napcat)


def record_recovery(evaluation, now):
    state = dict(evaluation.state)
    timestamps = list(state.get("recoveryTimestamps", []))
    timestamps.append(now)
    state["recoveryTimestamps"] = timestamps
    _prune_recoveries(state, now)
    state["lastRecoveryAt"] = utc_text(now)
    state["consecutiveOnebotFailures"] = 0
    state["overallState"] = "degraded"
    state["alertCode"] = None
    return Evaluation(state, False)


def deployment_in_progress(evaluation, now):
    state = dict(evaluation.state)
    state["checkedAt"] = utc_text(now)
    state["overallState"] = "degraded"
    state["alertCode"] = "deployment_in_progress"
    return Evaluation(state, False)


def public_state(state):
    fields = (
        "schemaVersion",
        "checkedAt",
        "overallState",
        "vaaState",
        "onebotState",
        "backupState",
        "latestBackupAt",
        "consecutiveOnebotFailures",
        "recoveriesInWindow",
        "lastRecoveryAt",
        "alertCode",
    )
    return {name: state.get(name) for name in fields}


def load_private_state(path, now):
    try:
        with open(path, "r") as state_file:
            return _safe_previous(json.load(state_file), now)
    except (IOError, OSError, TypeError, ValueError):
        return initial_state(now)


def atomic_write_json(path, payload, mode=0o640):
    directory = os.path.dirname(path)
    if directory:
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o750)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".cloud-monitor-", suffix=".json", dir=directory or None
    )
    try:
        with os.fdopen(descriptor, "w") as state_file:
            json.dump(payload, state_file, sort_keys=True, separators=(",", ":"))
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        if directory:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class SubprocessRunner(object):
    def _health(self, url, expected):
        try:
            response = urllib.request.urlopen(url, timeout=5)
            try:
                payload = json.load(response)
                status = response.getcode()
            finally:
                response.close()
        except (IOError, OSError, ValueError, urllib.error.URLError):
            return False
        return status == 200 and payload == {"status": expected}

    def observe_onebot(self, config):
        url = config.base_url + "/api/health/onebot"
        try:
            response = urllib.request.urlopen(url, timeout=5)
            try:
                payload = json.load(response)
                status = response.getcode()
            finally:
                response.close()
        except (IOError, OSError, ValueError, urllib.error.URLError):
            return "unknown"
        value = payload.get("status") if isinstance(payload, dict) else None
        return value if status == 200 and value in _ONEBOT_STATES else "unknown"

    def observe(self, config, now):
        live = self._health(config.base_url + "/api/health/live", "ok")
        if not live:
            vaa_state = "unavailable"
            onebot_state = "unknown"
        elif not self._health(
            config.base_url + "/api/health/ready", "ready"
        ):
            vaa_state = "not_ready"
            onebot_state = "unknown"
        else:
            vaa_state = "ready"
            onebot_state = self.observe_onebot(config)
        backup_state, latest_backup_at = self._backup(config.backup_directory, now)
        return Observation(
            vaa_state, onebot_state, backup_state, latest_backup_at
        )

    def _backup(self, directory, now):
        try:
            names = [
                os.path.join(directory, name)
                for name in os.listdir(directory)
                if name.startswith("assistant-") and name.endswith(".db")
            ]
            files = [path for path in names if os.path.isfile(path)]
            if not files:
                return "missing", None
            latest = max(files, key=os.path.getmtime)
            modified = os.path.getmtime(latest)
        except OSError:
            return "unknown", None
        state = "fresh" if now - modified <= BACKUP_STALE_SECONDS else "stale"
        return state, utc_text(modified)

    @contextlib.contextmanager
    def acquire_monitor_lock(self, path):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, 0o750)
        lock_file = open(path, "a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            lock_file.close()
            raise RuntimeError("monitor_already_running")
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def deployment_lock_busy(self, path):
        lock_file = open(path, "a+")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except IOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            lock_file.close()

    def restart_napcat(self, compose_file):
        subprocess.check_call(
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "restart",
                "napcat",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def run_once(config, runner=None, now=None):
    runner = runner or SubprocessRunner()
    now = now or time.time
    with runner.acquire_monitor_lock(config.monitor_lock_file):
        checked_at = now()
        previous = load_private_state(config.private_state_file, checked_at)
        observation = runner.observe(config, checked_at)
        evaluation = evaluate(previous, observation, checked_at)
        if evaluation.restart_napcat:
            if runner.deployment_lock_busy(config.deploy_lock_file):
                evaluation = deployment_in_progress(evaluation, checked_at)
            elif runner.observe_onebot(config) == "disconnected":
                try:
                    runner.restart_napcat(config.compose_file)
                except (OSError, subprocess.CalledProcessError):
                    state = dict(evaluation.state)
                    state["overallState"] = "alerting"
                    state["alertCode"] = "recovery_exhausted"
                    evaluation = Evaluation(state, False)
                else:
                    evaluation = record_recovery(evaluation, checked_at)
            else:
                evaluation = evaluate(
                    previous,
                    Observation(
                        observation.vaa_state,
                        "connected",
                        observation.backup_state,
                        observation.latest_backup_at,
                    ),
                    checked_at,
                )
        atomic_write_json(config.private_state_file, evaluation.state)
        atomic_write_json(config.public_state_file, public_state(evaluation.state))
        print("cloud_monitor=" + evaluation.state["overallState"])
        return evaluation


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("--public-state-file", required=True)
    parser.add_argument("--private-state-file", required=True)
    parser.add_argument("--backup-directory", required=True)
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8080"
    )
    parser.add_argument(
        "--monitor-lock-file",
        default="/opt/virtual-anime-assistant/cloud-monitor.lock",
    )
    parser.add_argument(
        "--deploy-lock-file",
        default="/opt/virtual-anime-assistant/deploy.lock",
    )
    return parser.parse_args(argv)


def main(argv=None):
    run_once(parse_args(argv))


if __name__ == "__main__":
    main()
