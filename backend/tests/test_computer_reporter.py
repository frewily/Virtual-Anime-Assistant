import asyncio
import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ComputerSnapshot
from computer.reporter import ComputerStateReporter, ReporterError


def snapshot(seconds: int = 0) -> ComputerSnapshot:
    collected_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc) + timedelta(
        seconds=seconds
    )
    return ComputerSnapshot(
        device_id="macbook-main",
        platform=ComputerPlatform.MACOS,
        collected_at=collected_at,
        expires_at=collected_at + timedelta(seconds=45),
        capabilities=frozenset({"system.resources"}),
        state={
            "system.resources": {
                "status": "available",
                "privateMarker": "snapshot-secret",
            }
        },
    )


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    async def wait(self) -> int:
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class BlockingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0
        self._reaped = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        super().kill()
        self._reaped.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._reaped.wait()
        self.waited = True
        return self.returncode


class FakeResponse:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error


class FakeHttpClient:
    def __init__(self, outcomes=None) -> None:
        self.outcomes = list(outcomes or ())
        self.requests = []
        self.closed = False

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    async def aclose(self) -> None:
        self.closed = True


class ComputerReporterTests(unittest.TestCase):
    def make_reporter(self, **overrides):
        current = overrides.pop("current", snapshot())
        process = overrides.pop("process", FakeProcess())
        process_calls = []

        async def process_factory(*args, **kwargs):
            process_calls.append((args, kwargs))
            return process

        client = overrides.pop("http_client", FakeHttpClient())
        values = {
            "latest_snapshot": lambda: current,
            "token": "report-token-secret",
            "ssh_target": "relay@cloud.example",
            "ssh_port": 2222,
            "identity_file": Path("/private/identity-secret"),
            "known_hosts_file": Path("/private/known-hosts-secret"),
            "local_port": 18080,
            "report_path": "/api/computer/state",
            "process_factory": process_factory,
            "http_client": client,
            "process_shutdown_timeout": 0.01,
        }
        values.update(overrides)
        return ComputerStateReporter(**values), process, process_calls, client

    def test_uses_exact_strict_ssh_argv_without_secret_environment(self):
        async def exercise():
            reporter, _, calls, _ = self.make_reporter()

            await reporter.open_tunnel()

            self.assertEqual(
                calls,
                [
                    (
                        (
                            "/usr/bin/ssh",
                            "-N",
                            "-F",
                            "none",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "IdentitiesOnly=yes",
                            "-o",
                            "StrictHostKeyChecking=yes",
                            "-o",
                            "ExitOnForwardFailure=yes",
                            "-o",
                            "ServerAliveInterval=30",
                            "-o",
                            "ServerAliveCountMax=3",
                            "-o",
                            "ProxyCommand=none",
                            "-o",
                            "ProxyJump=none",
                            "-o",
                            "PermitLocalCommand=no",
                            "-o",
                            "UserKnownHostsFile=/private/known-hosts-secret",
                            "-i",
                            "/private/identity-secret",
                            "-p",
                            "2222",
                            "-L",
                            "127.0.0.1:18080:127.0.0.1:8080",
                            "relay@cloud.example",
                        ),
                        {
                            "stdin": asyncio.subprocess.DEVNULL,
                            "stdout": asyncio.subprocess.DEVNULL,
                            "stderr": asyncio.subprocess.DEVNULL,
                        },
                    )
                ],
            )

        asyncio.run(exercise())

    def test_posts_only_aliased_snapshot_and_bearer_header_once_per_timestamp(self):
        async def exercise():
            reporter, _, _, client = self.make_reporter()

            self.assertTrue(await reporter.report_once())
            self.assertFalse(await reporter.report_once())

            self.assertEqual(len(client.requests), 1)
            url, request = client.requests[0]
            self.assertEqual(url, "http://127.0.0.1:18080/api/computer/state")
            self.assertEqual(
                request["json"], snapshot().model_dump(mode="json", by_alias=True)
            )
            self.assertEqual(
                request["headers"],
                {"Authorization": "Bearer report-token-secret"},
            )
            self.assertNotIn("report-token-secret", str(request["json"]))

        asyncio.run(exercise())

    def test_does_not_send_an_older_snapshot_after_a_newer_one(self):
        async def exercise():
            current = [snapshot(30)]
            reporter, _, _, client = self.make_reporter(
                latest_snapshot=lambda: current[0]
            )

            self.assertTrue(await reporter.report_once())
            current[0] = snapshot(15)
            self.assertFalse(await reporter.report_once())

            self.assertEqual(len(client.requests), 1)

        asyncio.run(exercise())

    def test_run_uses_fifteen_second_heartbeat_and_capped_exponential_backoff(self):
        async def exercise():
            sleeps = []
            reporter = None

            async def fake_sleep(delay):
                sleeps.append(delay)
                if len(sleeps) == 9:
                    reporter.stop()

            client = FakeHttpClient(
                [RuntimeError("private failure")] * 7 + [None]
            )
            reporter, _, _, _ = self.make_reporter(
                http_client=client,
                sleep=fake_sleep,
            )

            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                await reporter.run()

            self.assertEqual(sleeps, [1, 2, 4, 8, 16, 32, 60, 15, 15])
            self.assertEqual(len(client.requests), 8)
            self.assertEqual(len(logs.output), 7)

        asyncio.run(exercise())

    def test_close_terminates_and_waits_for_ssh_then_closes_http(self):
        async def exercise():
            reporter, process, _, client = self.make_reporter()
            await reporter.open_tunnel()

            await reporter.aclose()

            self.assertTrue(process.terminated)
            self.assertTrue(process.waited)
            self.assertFalse(client.closed)

        asyncio.run(exercise())

    def test_close_redacts_process_cleanup_failure_and_still_closes_http(self):
        async def exercise():
            class FailingCleanupProcess(BlockingProcess):
                def terminate(self):
                    raise RuntimeError(
                        "report-token-secret /private/identity-secret"
                    )

            process = FailingCleanupProcess()
            reporter, _, _, client = self.make_reporter(process=process)
            await reporter.open_tunnel()

            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                await reporter.aclose()

            self.assertTrue(process.waited)
            self.assertTrue(process.killed)
            self.assertFalse(client.closed)
            self.assertNotIn("report-token-secret", " ".join(logs.output))
            self.assertNotIn("/private/identity-secret", " ".join(logs.output))

        asyncio.run(exercise())

    def test_kills_and_reaps_process_that_does_not_exit_after_terminate(self):
        async def exercise():
            process = BlockingProcess()
            reporter, _, _, client = self.make_reporter(process=process)
            await reporter.open_tunnel()

            await reporter.aclose()

            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertGreaterEqual(process.wait_calls, 2)
            self.assertIsNone(reporter._process)
            self.assertFalse(client.closed)

        asyncio.run(exercise())

    def test_owned_http_client_is_closed_but_injected_client_is_not(self):
        async def exercise():
            external_reporter, _, _, external = self.make_reporter()
            await external_reporter.aclose()
            self.assertFalse(external.closed)

            owned_reporter = ComputerStateReporter(
                latest_snapshot=lambda: None,
                token="token",
                ssh_target="relay@cloud.example",
                ssh_port=22,
                identity_file=Path("/private/identity"),
                known_hosts_file=Path("/private/known-hosts"),
                local_port=18080,
            )
            owned = owned_reporter._http_client
            await owned_reporter.aclose()
            self.assertTrue(owned.is_closed)

        asyncio.run(exercise())

    def test_rejects_option_whitespace_control_and_non_user_host_targets(self):
        invalid_targets = (
            "-relay@cloud.example",
            "relay @cloud.example",
            "relay@cloud.example\nProxyCommand=evil",
            "relay@cloud.example\x00evil",
            "cloud.example",
            "relay@",
            "@cloud.example",
        )
        for target in invalid_targets:
            with self.subTest(target=repr(target)):
                with self.assertRaisesRegex(
                    ValueError, "^computer state SSH target is invalid$"
                ):
                    self.make_reporter(ssh_target=target)

    def test_concurrent_open_calls_create_only_one_process(self):
        async def exercise():
            process = FakeProcess()
            calls = 0

            async def process_factory(*args, **kwargs):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0)
                return process

            reporter, _, _, _ = self.make_reporter(
                process_factory=process_factory
            )

            await asyncio.gather(
                reporter.open_tunnel(),
                reporter.open_tunnel(),
            )

            self.assertEqual(calls, 1)
            await reporter.aclose()

        asyncio.run(exercise())

    def test_close_racing_with_open_does_not_lose_the_created_process(self):
        async def exercise():
            process = FakeProcess()
            started = asyncio.Event()
            release = asyncio.Event()

            async def process_factory(*args, **kwargs):
                started.set()
                await release.wait()
                return process

            reporter, _, _, client = self.make_reporter(
                process_factory=process_factory
            )
            opening = asyncio.create_task(reporter.open_tunnel())
            await started.wait()
            closing = asyncio.create_task(reporter.aclose())
            await asyncio.sleep(0)
            release.set()

            await opening
            await closing

            self.assertTrue(process.terminated)
            self.assertTrue(process.waited)
            self.assertIsNone(reporter._process)
            self.assertFalse(client.closed)

        asyncio.run(exercise())

    def test_cancelled_close_finishes_cleanup_then_reraises_cancellation(self):
        async def exercise():
            process = BlockingProcess()

            async def process_factory(*args, **kwargs):
                return process

            reporter = ComputerStateReporter(
                latest_snapshot=lambda: None,
                token="token",
                ssh_target="relay@cloud.example",
                ssh_port=22,
                identity_file=Path("/private/identity"),
                known_hosts_file=Path("/private/known-hosts"),
                local_port=18080,
                process_factory=process_factory,
                process_shutdown_timeout=0.01,
            )
            owned = reporter._http_client
            await reporter.open_tunnel()

            closing = asyncio.create_task(reporter.aclose())
            await asyncio.sleep(0)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing

            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertIsNone(reporter._process)
            self.assertTrue(reporter._closed)
            self.assertTrue(owned.is_closed)

        asyncio.run(exercise())

    def test_unreaped_process_reference_is_retained_after_bounded_shutdown(self):
        async def exercise():
            class UnreapableProcess(BlockingProcess):
                def kill(self):
                    self.killed = True

            process = UnreapableProcess()
            reporter, _, _, external = self.make_reporter(process=process)
            await reporter.open_tunnel()

            with self.assertLogs("computer.reporter", logging.WARNING):
                await reporter.aclose()

            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertIs(reporter._process, process)
            self.assertTrue(reporter._closed)
            self.assertFalse(external.closed)

        asyncio.run(exercise())

    def test_reaps_an_exited_tunnel_before_starting_its_replacement(self):
        async def exercise():
            first = FakeProcess()
            first.returncode = 255
            second = FakeProcess()
            processes = [first, second]

            async def process_factory(*args, **kwargs):
                return processes.pop(0)

            reporter, _, _, _ = self.make_reporter(
                process_factory=process_factory
            )

            await reporter.open_tunnel()
            await reporter.open_tunnel()

            self.assertTrue(first.waited)
            self.assertIs(reporter._process, second)

        asyncio.run(exercise())

    def test_failures_and_logs_do_not_disclose_token_paths_snapshot_or_cause(self):
        async def exercise():
            client = FakeHttpClient(
                [
                    RuntimeError(
                        "report-token-secret /private/identity-secret "
                        "snapshot-secret"
                    )
                ]
            )
            reporter, _, _, _ = self.make_reporter(http_client=client)

            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ) as raised:
                    await reporter.report_once()

            combined = " ".join(logs.output) + " " + str(raised.exception)
            for private_value in (
                "report-token-secret",
                "/private/identity-secret",
                "/private/known-hosts-secret",
                "snapshot-secret",
                "private failure",
            ):
                self.assertNotIn(private_value, combined)

        asyncio.run(exercise())

    def test_snapshot_reader_exception_is_redacted(self):
        async def exercise():
            def failing_reader():
                raise RuntimeError(
                    "report-token-secret /private/identity-secret snapshot-secret"
                )

            reporter, _, _, _ = self.make_reporter(
                latest_snapshot=failing_reader
            )

            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ) as raised:
                    await reporter.report_once()

            combined = " ".join(logs.output) + " " + str(raised.exception)
            self.assertNotIn("report-token-secret", combined)
            self.assertNotIn("/private/identity-secret", combined)
            self.assertNotIn("snapshot-secret", combined)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
