import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.macos import (
    AsyncProcessRunner,
    MacOSActionProvider,
    MacOSActionError,
    ProcessResult,
    build_macos_state_providers,
)


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def run(self, argv, *, timeout=3):
        self.calls.append(tuple(argv))
        response = self.responses.get(tuple(argv))
        if isinstance(response, BaseException):
            raise response
        return response or ProcessResult(returncode=1, stdout="", stderr="")


class FakePsutil:
    @staticmethod
    def cpu_percent(interval=None):
        return 21.5

    @staticmethod
    def virtual_memory():
        return type("Memory", (), {"percent": 61.25})()

    @staticmethod
    def disk_usage(path):
        assert path == "/"
        return type("Disk", (), {"percent": 72.0})()

    @staticmethod
    def sensors_battery():
        return type(
            "Battery",
            (),
            {"percent": 88.0, "power_plugged": True},
        )()


class BlockingStream:
    def __init__(self, chunks=()) -> None:
        self.chunks = list(chunks)
        self.blocked = asyncio.Event()

    async def read(self, _: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        await self.blocked.wait()
        return b""


class BlockingProcess:
    def __init__(self, *, stdout_chunks=()) -> None:
        self.stdout = BlockingStream(stdout_chunks)
        self.stderr = BlockingStream()
        self.returncode = None
        self.killed = False
        self.wait_calls = 0
        self._killed = asyncio.Event()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._killed.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._killed.wait()
        return self.returncode


class MacOSComputerTests(unittest.TestCase):
    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def build(self, responses):
        runner = FakeRunner(responses)
        providers = build_macos_state_providers(
            runner=runner,
            psutil_module=FakePsutil,
        )
        return runner, {provider.capability: provider for provider in providers}

    def test_process_runner_uses_exec_without_shell(self):
        source = inspect.getsource(AsyncProcessRunner.run)

        self.assertIn("create_subprocess_exec", source)
        self.assertNotIn("create_subprocess_shell", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("communicate", source)

    def test_process_runner_kills_and_reaps_when_output_exceeds_limit(self):
        process = BlockingProcess(
            stdout_chunks=(b"x" * (32 * 1024), b"y" * (32 * 1024), b"z")
        )

        with patch(
            "computer.macos.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            with self.assertRaises(RuntimeError):
                self.run_async(AsyncProcessRunner().run(("/usr/bin/true",)))

        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)

    def test_process_runner_kills_and_reaps_on_timeout(self):
        process = BlockingProcess()

        with patch(
            "computer.macos.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            with self.assertRaises(TimeoutError):
                self.run_async(
                    AsyncProcessRunner().run(
                        ("/usr/bin/true",),
                        timeout=0.001,
                    )
                )

        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)

    def test_resources_and_power_use_bounded_psutil_results(self):
        _, providers = self.build({})

        resources = self.run_async(providers["system.resources"].collect())
        power = self.run_async(providers["system.power"].collect())

        self.assertEqual(
            resources.state,
            {
                "status": "available",
                "cpuPercent": 21.5,
                "memoryPercent": 61.25,
                "diskPercent": 72.0,
            },
        )
        self.assertEqual(
            power.state,
            {"status": "available", "percent": 88, "charging": True},
        )

    def test_presence_uses_fixed_ioreg_commands_and_rounds_idle_minutes(self):
        idle_command = ("/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4")
        lock_command = ("/usr/sbin/ioreg", "-n", "Root", "-d", "1")
        runner, providers = self.build(
            {
                idle_command: ProcessResult(
                    returncode=0,
                    stdout='"HIDIdleTime" = 125000000000\n',
                    stderr="",
                ),
                lock_command: ProcessResult(
                    returncode=0,
                    stdout='"CGSSessionScreenIsLocked" = No\n',
                    stderr="",
                ),
            }
        )

        result = self.run_async(providers["system.presence"].collect())

        self.assertEqual(result.state["presence"], "active")
        self.assertEqual(result.state["idleMinutes"], 2)
        self.assertIn(idle_command, runner.calls)
        self.assertIn(lock_command, runner.calls)

    def test_foreground_is_privacy_filtered_before_result(self):
        command = next(
            provider.command
            for provider in build_macos_state_providers(
                runner=FakeRunner({}), psutil_module=FakePsutil
            )
            if provider.capability == "application.foreground"
        )
        _, providers = self.build(
            {
                command: ProcessResult(
                    returncode=0,
                    stdout=(
                        "Safari\x1fcom.apple.Safari\x1f"
                        "Secret banking page\x1fYES"
                    ),
                    stderr="",
                )
            }
        )

        result = self.run_async(
            providers["application.foreground"].collect()
        )

        self.assertEqual(result.state["appName"], "Safari")
        self.assertEqual(result.state["privacyLevel"], "browser")
        self.assertNotIn("windowTitle", result.state)
        self.assertTrue(result.state["fullscreen"])

    def test_network_media_and_volume_parse_only_fixed_outputs(self):
        seed = build_macos_state_providers(
            runner=FakeRunner({}), psutil_module=FakePsutil
        )
        commands = {
            provider.capability: provider.command
            for provider in seed
            if hasattr(provider, "command")
        }
        _, providers = self.build(
            {
                commands["system.network"]: ProcessResult(
                    0, "Network interfaces: en0\n", ""
                ),
                commands["media.playback"]: ProcessResult(
                    0, "Music\x1fplaying\x1fSong\x1fArtist", ""
                ),
                commands["media.volume"]: ProcessResult(0, "42\x1fNO", ""),
            }
        )

        network = self.run_async(providers["system.network"].collect())
        media = self.run_async(providers["media.playback"].collect())
        volume = self.run_async(providers["media.volume"].collect())

        self.assertEqual(network.state, {"status": "available", "online": True})
        self.assertEqual(media.state["player"], "Music")
        self.assertEqual(media.state["title"], "Song")
        self.assertEqual(volume.state["percent"], 42)
        self.assertFalse(volume.state["muted"])

    def test_failed_probe_returns_stable_unavailable_without_output(self):
        seed = build_macos_state_providers(
            runner=FakeRunner({}), psutil_module=FakePsutil
        )
        network_command = next(
            provider.command
            for provider in seed
            if provider.capability == "system.network"
        )
        _, providers = self.build(
            {
                network_command: ProcessResult(
                    returncode=1,
                    stdout="private output",
                    stderr="secret error",
                )
            }
        )

        result = self.run_async(providers["system.network"].collect())

        self.assertEqual(
            result.state,
            {"status": "unavailable", "errorCode": "state_probe_failed"},
        )
        self.assertNotIn("private output", str(result.state))
        self.assertNotIn("secret error", str(result.state))

    def test_open_application_uses_fixed_open_argv_for_name_and_bundle_id(self):
        runner = FakeRunner(
            {
                ("/usr/bin/open", "-a", "Safari"): ProcessResult(0, "", ""),
                ("/usr/bin/open", "-b", "com.apple.Safari"): ProcessResult(
                    0, "", ""
                ),
            }
        )
        actions = MacOSActionProvider(runner)

        self.run_async(actions.open_application("Safari"))
        self.run_async(actions.open_application("com.apple.Safari"))

        self.assertEqual(
            runner.calls,
            [
                ("/usr/bin/open", "-a", "Safari"),
                ("/usr/bin/open", "-b", "com.apple.Safari"),
            ],
        )

    def test_open_url_uses_fixed_open_argv(self):
        url = "https://example.com/docs"
        runner = FakeRunner(
            {("/usr/bin/open", url): ProcessResult(0, "", "")}
        )

        self.run_async(MacOSActionProvider(runner).open_url(url))

        self.assertEqual(runner.calls[-1], ("/usr/bin/open", url))

    def test_action_provider_revalidates_application_and_url_inputs(self):
        runner = FakeRunner({})
        actions = MacOSActionProvider(runner)

        for application in ("../Safari", "-a", "Safari\u2028Calculator"):
            with self.subTest(application=application):
                with self.assertRaises(MacOSActionError):
                    self.run_async(actions.open_application(application))
        for url in (
            "https://localhost/docs",
            "https://intranet/docs",
            "https://example.com\\docs",
        ):
            with self.subTest(url=url):
                with self.assertRaises(MacOSActionError):
                    self.run_async(actions.open_url(url))

        self.assertEqual(runner.calls, [])

    def test_action_provider_opens_canonical_public_https_url(self):
        canonical = "https://xn--bcher-kva.example/Docs?q=1"
        runner = FakeRunner(
            {("/usr/bin/open", canonical): ProcessResult(0, "", "")}
        )

        self.run_async(
            MacOSActionProvider(runner).open_url(
                "HTTPS://BÜCHER.example/Docs?q=1"
            )
        )

        self.assertEqual(runner.calls, [("/usr/bin/open", canonical)])

    def test_media_player_uses_only_player_specific_fixed_scripts(self):
        runner = FakeRunner({})
        actions = MacOSActionProvider(runner)

        with self.assertRaises(RuntimeError):
            self.run_async(actions.toggle_media("Music"))
        music_command = runner.calls[-1]
        with self.assertRaises(RuntimeError):
            self.run_async(actions.toggle_media("Spotify"))
        spotify_command = runner.calls[-1]

        self.assertEqual(music_command[0:2], ("/usr/bin/osascript", "-e"))
        self.assertEqual(spotify_command[0:2], ("/usr/bin/osascript", "-e"))
        self.assertIn('application "Music"', music_command[2])
        self.assertNotIn("Spotify", music_command[2])
        self.assertIn('application "Spotify"', spotify_command[2])
        self.assertNotIn("Music", spotify_command[2])

    def test_set_volume_uses_bounded_integer_in_fixed_template(self):
        command = (
            "/usr/bin/osascript",
            "-e",
            "set volume output volume 42",
        )
        runner = FakeRunner({command: ProcessResult(0, "", "")})

        self.run_async(MacOSActionProvider(runner).set_volume(42))

        self.assertEqual(runner.calls[-1], command)


if __name__ == "__main__":
    unittest.main()
