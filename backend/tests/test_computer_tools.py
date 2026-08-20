import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ComputerSnapshot, ModelAccess
from computer.macos import MacOSActionError, MacOSActionProvider, ProcessResult
from computer.tools import (
    EmptyComputerArguments,
    MediaPlayer,
    OpenApplicationArguments,
    OpenUrlArguments,
    SetVolumeArguments,
    ToggleMediaArguments,
    build_current_state_tool,
    build_macos_action_tools,
    summarize_open_url,
)
from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from tools.catalog import ModelToolCatalog
from tools.registry import ToolDefinition, ToolRegistry
from tools.service import ToolExecutionError


class FakeStateReader:
    def __init__(self, snapshot=None, *, stale=False) -> None:
        self.snapshot = snapshot
        self.stale = stale

    def latest(self):
        if self.snapshot is None:
            return None
        return self.snapshot.model_copy(deep=True)

    def is_stale(self):
        return self.stale


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, argv, *, timeout=3):
        self.calls.append(tuple(argv))
        return ProcessResult(0, "", "")


def snapshot() -> ComputerSnapshot:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    return ComputerSnapshot(
        device_id="macbook-main",
        platform=ComputerPlatform.MACOS,
        collected_at=now,
        expires_at=now + timedelta(seconds=45),
        capabilities=frozenset({"system.resources", "system.network"}),
        state={
            "system.resources": {
                "status": "available",
                "cpuPercent": 12,
            },
            "system.network": {
                "status": "unavailable",
                "errorCode": "state_probe_failed",
            },
        },
    )


class ComputerToolTests(unittest.TestCase):
    def test_current_state_is_low_risk_model_tool_scoped_to_desktop(self):
        registry = ToolRegistry()
        registry.register(
            build_current_state_tool(
                FakeStateReader(snapshot()),
                allowed_channels=frozenset({MessageSource.DESKTOP}),
            )
        )
        catalog = ModelToolCatalog(registry)

        desktop = catalog.list(MessageSource.DESKTOP)
        qq = catalog.list(MessageSource.QQ)

        self.assertEqual([tool.name for tool in desktop], ["computer.current_state"])
        self.assertEqual(qq, ())
        definition = registry.require("computer.current_state")
        self.assertIs(definition.risk, ToolRisk.LOW)
        self.assertIs(definition.model_access, ModelAccess.READ_ONLY)
        self.assertIn(ToolSource.MODEL, definition.allowed_sources)

    def test_current_state_returns_safe_snapshot_and_stale_partition_summary(self):
        definition = build_current_state_tool(
            FakeStateReader(snapshot(), stale=True),
            allowed_channels=frozenset({MessageSource.QQ}),
        )

        result = asyncio.run(definition.handler(EmptyComputerArguments()))

        self.assertEqual(result["freshness"], "stale")
        self.assertEqual(result["deviceId"], "macbook-main")
        self.assertEqual(result["unavailableCapabilities"], ["system.network"])
        self.assertEqual(result["state"]["system.resources"]["cpuPercent"], 12)

    def test_current_state_missing_snapshot_returns_stable_error(self):
        definition = build_current_state_tool(
            FakeStateReader(),
            allowed_channels=frozenset({MessageSource.DESKTOP}),
        )

        with self.assertRaises(ToolExecutionError) as raised:
            asyncio.run(definition.handler(EmptyComputerArguments()))

        self.assertEqual(
            raised.exception.error_code, "computer_state_unavailable"
        )

    def test_macos_actions_are_high_risk_model_proposals_for_desktop_only(self):
        registry = ToolRegistry()
        for definition in build_macos_action_tools(
            MacOSActionProvider(FakeRunner())
        ):
            registry.register(definition)

        self.assertEqual(
            [definition.name for definition in registry.list()],
            [
                "computer.open_application",
                "computer.open_url",
                "computer.set_volume",
                "computer.toggle_media",
            ],
        )
        for definition in registry.list():
            self.assertIs(definition.risk, ToolRisk.HIGH)
            self.assertIs(
                definition.model_access,
                ModelAccess.PROPOSE_WITH_CONFIRMATION,
            )
            self.assertEqual(definition.allowed_sources, {ToolSource.MODEL})
            self.assertEqual(
                definition.allowed_channels, {MessageSource.DESKTOP}
            )
        self.assertEqual(
            registry.require("computer.open_url").sensitive_fields,
            {"url"},
        )

    def test_open_application_rejects_paths_options_controls_and_overlength(self):
        valid = ["Safari", "Visual Studio Code", "com.apple.Safari"]
        invalid = [
            "-a",
            "/Applications/Safari.app",
            r"Applications\Safari.app",
            "Safari\nCalculator",
            "Safari\x00",
            "Safari\u200bCalculator",
            "Safari\u2028Calculator",
            "Safari\u2029Calculator",
            "a" * 101,
        ]

        for application in valid:
            with self.subTest(application=application):
                self.assertEqual(
                    OpenApplicationArguments(application=application).application,
                    application,
                )
        for application in invalid:
            with self.subTest(application=application):
                with self.assertRaises(ValidationError):
                    OpenApplicationArguments(application=application)

    def test_open_url_accepts_only_public_absolute_https_without_fragment(self):
        valid = "https://example.com/docs?token=secret&empty="
        invalid = [
            "http://example.com",
            "https://user:pass@example.com/docs",
            "https://example.com/docs#section",
            "https://127.0.0.1/docs",
            "https://127.1/docs",
            "https://2130706433/docs",
            "https://%31%32%37.0.0.1/docs",
            "https://127%2e0%2e0%2e1/docs",
            "https://[::1]/docs",
            "https://localhost/docs",
            "https://api.localhost/docs",
            "https://local%68ost/docs",
            "https://printer.local/docs",
            "https://intranet/docs",
            "https://example.com\\docs",
            "https://bad_host.example/docs",
            "https://-bad.example/docs",
            "https:///relative",
            "https://example.com/\nsecret",
            "https://example.com/\u00a0secret",
            "https://example.com/\u200bsecret",
            "https://example.com/\u2028secret",
            "https://example.com/\u2029secret",
            "https://example.com/" + "a" * 2040,
        ]

        self.assertEqual(OpenUrlArguments(url=valid).url, valid)
        self.assertEqual(
            OpenUrlArguments(
                url="HTTPS://BÜCHER.example/Docs?q=1"
            ).url,
            "https://xn--bcher-kva.example/Docs?q=1",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValidationError):
                    OpenUrlArguments(url=url)

    def test_open_url_confirmation_summary_hides_query_values(self):
        self.assertEqual(
            summarize_open_url(
                "https://EXAMPLE.com/docs?q=private&token=secret&flag"
            ),
            "https://example.com/docs?q=[已隐藏]&token=[已隐藏]&flag=[已隐藏]",
        )

    def test_media_player_is_closed_enum_and_volume_is_strict_integer(self):
        self.assertIs(
            ToggleMediaArguments(player="Music").player,
            MediaPlayer.MUSIC,
        )
        self.assertIs(
            ToggleMediaArguments(player="Spotify").player,
            MediaPlayer.SPOTIFY,
        )
        for player in ("VLC", "music", "Spotify\nMusic"):
            with self.subTest(player=player):
                with self.assertRaises(ValidationError):
                    ToggleMediaArguments(player=player)
        for volume in (0, 100, 42):
            self.assertEqual(SetVolumeArguments(volume=volume).volume, volume)
        for volume in (-1, 101, True, 42.0, "42"):
            with self.subTest(volume=volume):
                with self.assertRaises(ValidationError):
                    SetVolumeArguments(volume=volume)

    def test_action_handlers_execute_only_validated_arguments(self):
        runner = FakeRunner()
        definitions = {
            definition.name: definition
            for definition in build_macos_action_tools(
                MacOSActionProvider(runner)
            )
        }

        asyncio.run(
            definitions["computer.open_application"].handler(
                OpenApplicationArguments(application="Safari")
            )
        )
        asyncio.run(
            definitions["computer.open_url"].handler(
                OpenUrlArguments(url="https://example.com/docs")
            )
        )
        asyncio.run(
            definitions["computer.toggle_media"].handler(
                ToggleMediaArguments(player="Music")
            )
        )
        asyncio.run(
            definitions["computer.set_volume"].handler(
                SetVolumeArguments(volume=25)
            )
        )

        self.assertEqual(runner.calls[0], ("/usr/bin/open", "-a", "Safari"))
        self.assertEqual(
            runner.calls[1], ("/usr/bin/open", "https://example.com/docs")
        )
        self.assertEqual(runner.calls[2][0:2], ("/usr/bin/osascript", "-e"))
        self.assertEqual(
            runner.calls[3],
            ("/usr/bin/osascript", "-e", "set volume output volume 25"),
        )

    def test_action_handlers_translate_provider_errors_to_stable_tool_error(self):
        class FailingActions:
            async def open_application(self, application):
                raise MacOSActionError()

            async def open_url(self, url):
                raise MacOSActionError()

            async def toggle_media(self, player):
                raise MacOSActionError()

            async def set_volume(self, volume):
                raise MacOSActionError()

        definitions = {
            definition.name: definition
            for definition in build_macos_action_tools(FailingActions())
        }
        cases = (
            (
                "computer.open_application",
                OpenApplicationArguments(application="Safari"),
            ),
            (
                "computer.open_url",
                OpenUrlArguments(url="https://example.com/docs"),
            ),
            (
                "computer.toggle_media",
                ToggleMediaArguments(player="Music"),
            ),
            ("computer.set_volume", SetVolumeArguments(volume=25)),
        )

        for name, arguments in cases:
            with self.subTest(name=name), self.assertRaises(
                ToolExecutionError
            ) as raised:
                asyncio.run(definitions[name].handler(arguments))
            self.assertEqual(raised.exception.error_code, "macos_action_failed")

    def test_tool_definition_rejects_invalid_security_metadata_types(self):
        registered = build_current_state_tool(
            FakeStateReader(snapshot()),
            allowed_channels=frozenset({MessageSource.DESKTOP}),
        )
        invalid = (
            {"risk": "low"},
            {"cancellable": 1},
            {"sensitive_fields": frozenset({1})},
        )

        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                ToolDefinition(**{**registered.__dict__, **changes})


if __name__ == "__main__":
    unittest.main()
