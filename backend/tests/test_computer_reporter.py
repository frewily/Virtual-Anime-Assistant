import asyncio
import json
import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ComputerSnapshot
from computer.reporter import ComputerStateReporter, ReporterError


def snapshot(seconds: int = 0, *, marker: str = "snapshot-secret") -> ComputerSnapshot:
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
                "privateMarker": marker,
            }
        },
    )


class FakeProcess:
    def __init__(self, *, exit_code: int = 0, error: Exception | None = None) -> None:
        self.returncode = None
        self.exit_code = exit_code
        self.error = error
        self.communicated_input: bytes | None = None
        self.terminated = False
        self.killed = False
        self.waited = False

    async def communicate(self, input: bytes | None = None):
        self.communicated_input = input
        if self.error is not None:
            raise self.error
        self.returncode = self.exit_code
        return None, None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return 0 if self.returncode is None else self.returncode


class BlockingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.communicate_started = asyncio.Event()
        self._reaped = asyncio.Event()
        self.wait_calls = 0

    async def communicate(self, input: bytes | None = None):
        self.communicated_input = input
        self.communicate_started.set()
        await asyncio.Event().wait()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._reaped.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._reaped.wait()
        self.waited = True
        return -9


class ComputerReporterTests(unittest.TestCase):
    def make_reporter(self, **overrides):
        current = overrides.pop("current", snapshot())
        processes = list(overrides.pop("processes", [FakeProcess()]))
        process_calls = []

        async def process_factory(*args, **kwargs):
            process_calls.append((args, kwargs))
            if not processes:
                raise RuntimeError("private process factory failure")
            return processes.pop(0)

        values = {
            "latest_snapshot": lambda: current,
            "ssh_target": "relay@cloud.example",
            "ssh_port": 2222,
            "identity_file": Path("/private/identity-secret"),
            "known_hosts_file": Path("/private/known-hosts-secret"),
            "process_factory": process_factory,
            "process_timeout": 0.05,
            "process_shutdown_timeout": 0.01,
        }
        values.update(overrides)
        return ComputerStateReporter(**values), process_calls

    def test_uses_exact_no_forwarding_ssh_argv_and_sends_snapshot_on_stdin(self):
        async def exercise():
            process = FakeProcess()
            reporter, calls = self.make_reporter(processes=[process])

            self.assertTrue(await reporter.report_once())

            self.assertEqual(
                calls,
                [
                    (
                        (
                            "/usr/bin/ssh",
                            "-T",
                            "-F",
                            "none",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "IdentitiesOnly=yes",
                            "-o",
                            "StrictHostKeyChecking=yes",
                            "-o",
                            "ClearAllForwardings=yes",
                            "-o",
                            "ProxyCommand=none",
                            "-o",
                            "ProxyJump=none",
                            "-o",
                            "PermitLocalCommand=no",
                            "-o",
                            "RequestTTY=no",
                            "-o",
                            'UserKnownHostsFile="/private/known-hosts-secret"',
                            "-i",
                            "/private/identity-secret",
                            "-p",
                            "2222",
                            "relay@cloud.example",
                        ),
                        {
                            "stdin": asyncio.subprocess.PIPE,
                            "stdout": asyncio.subprocess.DEVNULL,
                            "stderr": asyncio.subprocess.DEVNULL,
                        },
                    )
                ],
            )
            self.assertIsNotNone(process.communicated_input)
            self.assertLessEqual(len(process.communicated_input), 32 * 1024)
            self.assertEqual(
                json.loads(process.communicated_input),
                snapshot().model_dump(mode="json", by_alias=True),
            )

        asyncio.run(exercise())

    def test_quotes_known_hosts_paths_for_openssh_option_parsing(self):
        reporter, _ = self.make_reporter(
            known_hosts_file=Path(
                "/Users/example/Library/Application Support/Assistant/known_hosts"
            )
        )

        self.assertIn(
            'UserKnownHostsFile="/Users/example/Library/Application Support/'
            'Assistant/known_hosts"',
            reporter.ssh_argv,
        )

    def test_does_not_repeat_same_or_older_snapshot(self):
        async def exercise():
            current = [snapshot(30)]
            reporter, calls = self.make_reporter(
                latest_snapshot=lambda: current[0],
                processes=[FakeProcess()],
            )

            self.assertTrue(await reporter.report_once())
            self.assertFalse(await reporter.report_once())
            current[0] = snapshot(15)
            self.assertFalse(await reporter.report_once())
            self.assertEqual(len(calls), 1)

        asyncio.run(exercise())

    def test_concurrent_reports_send_each_timestamp_once(self):
        async def exercise():
            reporter, calls = self.make_reporter(processes=[FakeProcess()])
            results = await asyncio.gather(
                reporter.report_once(),
                reporter.report_once(),
            )
            self.assertEqual(results, [True, False])
            self.assertEqual(len(calls), 1)

        asyncio.run(exercise())

    def test_rejects_payload_over_32_kib_before_starting_ssh(self):
        async def exercise():
            reporter, calls = self.make_reporter(
                current=snapshot(marker="x" * (33 * 1024))
            )
            with self.assertRaisesRegex(
                ReporterError, "^computer state report failed$"
            ):
                await reporter.report_once()
            self.assertEqual(calls, [])

        asyncio.run(exercise())

    def test_nonzero_exit_and_process_creation_failure_are_redacted(self):
        async def exercise():
            nonzero, _ = self.make_reporter(processes=[FakeProcess(exit_code=255)])
            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ):
                    await nonzero.report_once()
            self.assertNotIn("snapshot-secret", " ".join(logs.output))

            creation, _ = self.make_reporter(processes=[])
            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ):
                    await creation.report_once()
            combined = " ".join(logs.output)
            self.assertNotIn("private process factory failure", combined)
            self.assertNotIn("/private/identity-secret", combined)

        asyncio.run(exercise())

    def test_timeout_terminates_kills_and_reaps_process(self):
        async def exercise():
            process = BlockingProcess()
            reporter, _ = self.make_reporter(
                processes=[process],
                process_timeout=0.001,
            )
            with self.assertLogs("computer.reporter", logging.WARNING):
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ):
                    await reporter.report_once()
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertGreaterEqual(process.wait_calls, 2)

        asyncio.run(exercise())

    def test_cancelled_report_cleans_up_process_and_reraises(self):
        async def exercise():
            process = BlockingProcess()
            reporter, _ = self.make_reporter(
                processes=[process],
                process_timeout=30,
            )
            reporting = asyncio.create_task(reporter.report_once())
            await process.communicate_started.wait()
            reporting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await reporting
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)

        asyncio.run(exercise())

    def test_run_uses_heartbeat_and_capped_exponential_backoff(self):
        async def exercise():
            sleeps = []
            reporter = None
            processes = [FakeProcess(exit_code=255) for _ in range(7)]
            processes.append(FakeProcess())

            async def fake_sleep(delay):
                sleeps.append(delay)
                if len(sleeps) == 9:
                    reporter.stop()

            reporter, calls = self.make_reporter(
                processes=processes,
                sleep=fake_sleep,
            )
            with self.assertLogs("computer.reporter", logging.WARNING):
                await reporter.run()
            self.assertEqual(sleeps, [1, 2, 4, 8, 16, 32, 60, 15, 15])
            self.assertEqual(len(calls), 8)

        asyncio.run(exercise())

    def test_aclose_cancels_owned_loop_and_reaps_active_process(self):
        async def exercise():
            process = BlockingProcess()
            reporter, _ = self.make_reporter(
                processes=[process],
                process_timeout=30,
            )
            self.assertTrue(reporter.start())
            await process.communicate_started.wait()
            await reporter.aclose()
            self.assertFalse(reporter.running)
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            with self.assertRaisesRegex(
                ReporterError, "^computer state reporter is closed$"
            ):
                await reporter.report_once()

        asyncio.run(exercise())

    def test_cancelled_close_finishes_cleanup_then_reraises(self):
        async def exercise():
            process = BlockingProcess()
            reporter, _ = self.make_reporter(
                processes=[process],
                process_timeout=30,
            )
            reporter.start()
            await process.communicate_started.wait()
            closing = asyncio.create_task(reporter.aclose())
            await asyncio.sleep(0)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            self.assertTrue(process.killed)
            self.assertTrue(process.waited)
            self.assertTrue(reporter._closed)

        asyncio.run(exercise())

    def test_rejects_invalid_target_port_and_timeout(self):
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
        with self.assertRaisesRegex(
            ValueError, "^computer state SSH port is invalid$"
        ):
            self.make_reporter(ssh_port=0)
        with self.assertRaisesRegex(
            ValueError, "^computer state process timeout is invalid$"
        ):
            self.make_reporter(process_timeout=0)

    def test_snapshot_reader_failure_hides_paths_snapshot_and_cause(self):
        async def exercise():
            def failing_reader():
                raise RuntimeError(
                    "/private/identity-secret snapshot-secret private cause"
                )

            reporter, _ = self.make_reporter(latest_snapshot=failing_reader)
            with self.assertLogs("computer.reporter", logging.WARNING) as logs:
                with self.assertRaisesRegex(
                    ReporterError, "^computer state report failed$"
                ) as raised:
                    await reporter.report_once()
            combined = " ".join(logs.output) + str(raised.exception)
            for secret in (
                "/private/identity-secret",
                "/private/known-hosts-secret",
                "snapshot-secret",
                "private cause",
            ):
                self.assertNotIn(secret, combined)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
