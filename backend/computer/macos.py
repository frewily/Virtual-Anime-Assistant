"""Bounded macOS state probes built from fixed command argument arrays."""

import asyncio
import re
from dataclasses import dataclass
from typing import Protocol

import psutil

from computer.models import ProviderResult
from computer.privacy import sanitize_foreground


_MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
_SEPARATOR = "\x1f"
_FOREGROUND_SCRIPT = """tell application "System Events"
set frontApp to first application process whose frontmost is true
set appName to name of frontApp
set bundleId to ""
set windowTitle to ""
set fullScreen to "NO"
try
  set bundleId to bundle identifier of frontApp
end try
try
  set windowTitle to name of front window of frontApp
  if value of attribute "AXFullScreen" of front window of frontApp then set fullScreen to "YES"
end try
return appName & (character id 31) & bundleId & (character id 31) & windowTitle & (character id 31) & fullScreen
end tell"""
_MEDIA_SCRIPT = """tell application "System Events"
if exists process "Music" then
  tell application "Music"
    if player state is playing then return "Music" & (character id 31) & "playing" & (character id 31) & name of current track & (character id 31) & artist of current track
    return "Music" & (character id 31) & "paused" & (character id 31) & "" & (character id 31) & ""
  end tell
end if
if exists process "Spotify" then
  tell application "Spotify"
    if player state is playing then return "Spotify" & (character id 31) & "playing" & (character id 31) & name of current track & (character id 31) & artist of current track
    return "Spotify" & (character id 31) & "paused" & (character id 31) & "" & (character id 31) & ""
  end tell
end if
return "" & (character id 31) & "unavailable" & (character id 31) & "" & (character id 31) & ""
end tell"""
_VOLUME_SCRIPT = """set settingsValue to get volume settings
return (output volume of settingsValue as text) & (character id 31) & (output muted of settingsValue as text)"""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = 3,
    ) -> ProcessResult: ...


class AsyncProcessRunner:
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = 3,
    ) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return ProcessResult(
            returncode=process.returncode or 0,
            stdout=stdout[:_MAX_PROCESS_OUTPUT_BYTES].decode("utf-8", "replace"),
            stderr=stderr[:_MAX_PROCESS_OUTPUT_BYTES].decode("utf-8", "replace"),
        )


def _unavailable(code: str = "state_probe_failed") -> dict[str, str]:
    return {"status": "unavailable", "errorCode": code}


def _bounded_text(value: str, limit: int = 128) -> str:
    return " ".join(value.replace("\x00", " ").split())[:limit]


class _CommandProvider:
    capability: str
    command: tuple[str, ...]

    def __init__(self, runner: ProcessRunner) -> None:
        self.runner = runner

    async def collect(self) -> ProviderResult:
        try:
            result = await self.runner.run(self.command, timeout=3)
        except TimeoutError:
            state = _unavailable("state_probe_timeout")
        except Exception:
            state = _unavailable()
        else:
            if result.returncode != 0:
                state = _unavailable()
            else:
                try:
                    state = self.parse(result.stdout)
                except (TypeError, ValueError, IndexError):
                    state = _unavailable("state_probe_invalid")
        return ProviderResult(capability=self.capability, state=state)

    def parse(self, output: str) -> dict:
        raise NotImplementedError


class MacOSResourcesProvider:
    capability = "system.resources"

    def __init__(self, psutil_module) -> None:
        self.psutil = psutil_module

    async def collect(self) -> ProviderResult:
        try:
            state = {
                "status": "available",
                "cpuPercent": round(float(self.psutil.cpu_percent(interval=None)), 2),
                "memoryPercent": round(float(self.psutil.virtual_memory().percent), 2),
                "diskPercent": round(float(self.psutil.disk_usage("/").percent), 2),
            }
        except Exception:
            state = _unavailable()
        return ProviderResult(capability=self.capability, state=state)


class MacOSPowerProvider:
    capability = "system.power"

    def __init__(self, psutil_module) -> None:
        self.psutil = psutil_module

    async def collect(self) -> ProviderResult:
        try:
            battery = self.psutil.sensors_battery()
            state = (
                {"status": "not_applicable"}
                if battery is None
                else {
                    "status": "available",
                    "percent": max(0, min(100, int(round(battery.percent)))),
                    "charging": bool(battery.power_plugged),
                }
            )
        except Exception:
            state = _unavailable()
        return ProviderResult(capability=self.capability, state=state)


class MacOSPresenceProvider(_CommandProvider):
    capability = "system.presence"
    command = ("/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4")
    lock_command = ("/usr/sbin/ioreg", "-n", "Root", "-d", "1")

    async def collect(self) -> ProviderResult:
        try:
            idle = await self.runner.run(self.command, timeout=3)
            locked = await self.runner.run(self.lock_command, timeout=3)
            if idle.returncode != 0 or locked.returncode != 0:
                raise ValueError("probe failed")
            match = re.search(r'HIDIdleTime"\s*=\s*(\d+)', idle.stdout)
            if match is None:
                raise ValueError("idle time missing")
            idle_seconds = int(match.group(1)) / 1_000_000_000
            is_locked = bool(
                re.search(
                    r'CGSSessionScreenIsLocked"\s*=\s*(?:Yes|true|1)',
                    locked.stdout,
                    re.IGNORECASE,
                )
            )
            state = {
                "status": "available",
                "presence": (
                    "locked" if is_locked else "idle" if idle_seconds >= 300 else "active"
                ),
                "idleMinutes": int(idle_seconds // 60),
            }
        except TimeoutError:
            state = _unavailable("state_probe_timeout")
        except Exception:
            state = _unavailable()
        return ProviderResult(capability=self.capability, state=state)


class MacOSForegroundProvider(_CommandProvider):
    capability = "application.foreground"
    command = ("/usr/bin/osascript", "-e", _FOREGROUND_SCRIPT)

    def parse(self, output: str) -> dict:
        app_name, bundle_id, title, fullscreen = output.strip().split(_SEPARATOR, 3)
        safe = sanitize_foreground(
            app_name,
            title,
            bundle_id=bundle_id,
            fullscreen=fullscreen.strip().casefold() == "yes",
        )
        state = {
            "status": "available",
            "appName": safe.app_name,
            "privacyLevel": safe.privacy_level.value,
            "fullscreen": safe.fullscreen,
        }
        if safe.window_title is not None:
            state["windowTitle"] = safe.window_title
        return state


class MacOSNetworkProvider(_CommandProvider):
    capability = "system.network"
    command = ("/usr/sbin/scutil", "--nwi")

    def parse(self, output: str) -> dict:
        online = "Network interfaces:" in output and "No network" not in output
        return {"status": "available", "online": online}


class MacOSMediaProvider(_CommandProvider):
    capability = "media.playback"
    command = ("/usr/bin/osascript", "-e", _MEDIA_SCRIPT)

    def parse(self, output: str) -> dict:
        player, status, title, artist = output.strip().split(_SEPARATOR, 3)
        state = {"status": status or "unavailable"}
        if player:
            state["player"] = _bounded_text(player, 50)
        if title:
            state["title"] = _bounded_text(title)
        if artist:
            state["artist"] = _bounded_text(artist)
        return state


class MacOSVolumeProvider(_CommandProvider):
    capability = "media.volume"
    command = ("/usr/bin/osascript", "-e", _VOLUME_SCRIPT)

    def parse(self, output: str) -> dict:
        percent_text, muted_text = output.strip().split(_SEPARATOR, 1)
        percent = int(percent_text)
        if not 0 <= percent <= 100:
            raise ValueError("volume out of bounds")
        return {
            "status": "available",
            "percent": percent,
            "muted": muted_text.strip().casefold() in {"yes", "true", "1"},
        }


def build_macos_state_providers(
    *,
    runner: ProcessRunner | None = None,
    psutil_module=psutil,
) -> tuple:
    process_runner = runner or AsyncProcessRunner()
    return (
        MacOSResourcesProvider(psutil_module),
        MacOSPowerProvider(psutil_module),
        MacOSPresenceProvider(process_runner),
        MacOSForegroundProvider(process_runner),
        MacOSNetworkProvider(process_runner),
        MacOSMediaProvider(process_runner),
        MacOSVolumeProvider(process_runner),
    )
