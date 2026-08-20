"""Bounded macOS state probes built from fixed command argument arrays."""

import asyncio
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

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
_TOGGLE_MEDIA_SCRIPTS = {
    "Music": 'tell application "Music" to playpause',
    "Spotify": 'tell application "Spotify" to playpause',
}
_SET_VOLUME_SCRIPT = "set volume output volume {volume}"
_BUNDLE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)
_PUBLIC_DNS_LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


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
        stdout_task = asyncio.create_task(
            _read_bounded_stream(process.stdout)
        )
        stderr_task = asyncio.create_task(
            _read_bounded_stream(process.stderr)
        )
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            stdout, stderr, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=timeout,
            )
        except BaseException:
            await _kill_cancel_and_reap(process, tasks)
            raise
        return ProcessResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
        )


class ProcessOutputLimitError(RuntimeError):
    """Raised without retaining output when a child exceeds its byte budget."""

    def __init__(self) -> None:
        super().__init__("process_output_limit_exceeded")


async def _read_bounded_stream(stream) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(16 * 1024)
        if not chunk:
            return bytes(output)
        if len(output) + len(chunk) > _MAX_PROCESS_OUTPUT_BYTES:
            raise ProcessOutputLimitError()
        output.extend(chunk)


async def _kill_cancel_and_reap(process, tasks: tuple[asyncio.Task, ...]) -> None:
    if process.returncode is None:
        process.kill()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await process.wait()


def validate_application_identifier(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError("application identifier is invalid")
    if (
        value != value.strip()
        or value.startswith("-")
        or "/" in value
        or "\\" in value
        or any(_is_forbidden_application_char(char) for char in value)
    ):
        raise ValueError("application identifier is invalid")
    return value


def normalize_public_https_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("URL is invalid")
    if "\\" in value or any(_is_forbidden_url_char(char) for char in value):
        raise ValueError("URL contains a forbidden character")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "%" in parsed.netloc
        or "%" in hostname
        or "#" in value
    ):
        raise ValueError("URL must be an absolute HTTPS URL")

    unicode_host = hostname.casefold().rstrip(".")
    if (
        unicode_host == "localhost"
        or unicode_host.endswith(".localhost")
        or unicode_host.endswith(".local")
    ):
        raise ValueError("local hosts are not allowed")
    _reject_ip_literal(unicode_host)
    try:
        ascii_host = unicode_host.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("URL hostname is invalid") from exc
    if (
        len(ascii_host) > 253
        or "." not in ascii_host
        or ascii_host == "localhost"
        or ascii_host.endswith(".localhost")
        or ascii_host.endswith(".local")
        or any(
            _PUBLIC_DNS_LABEL_PATTERN.fullmatch(label) is None
            for label in ascii_host.split(".")
        )
    ):
        raise ValueError("URL hostname is not public DNS")
    _reject_ip_literal(ascii_host)

    canonical_netloc = ascii_host
    if port is not None:
        canonical_netloc = f"{canonical_netloc}:{port}"
    return urlunsplit(
        ("https", canonical_netloc, parsed.path, parsed.query, "")
    )


def _is_forbidden_application_char(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith("C") or category in {"Zl", "Zp"}


def _is_forbidden_url_char(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("C")


def _reject_ip_literal(hostname: str) -> None:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not allowed")
    try:
        socket.inet_aton(hostname)
    except OSError:
        return
    raise ValueError("IPv4 literals are not allowed")


class MacOSActionError(RuntimeError):
    """Stable failure raised by a bounded macOS action."""

    error_code = "macos_action_failed"

    def __init__(self) -> None:
        super().__init__(self.error_code)


class MacOSActionProvider:
    """Execute only the four explicitly supported macOS actions."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or AsyncProcessRunner()

    async def open_application(self, application: str) -> dict[str, str]:
        try:
            safe_application = validate_application_identifier(application)
        except (TypeError, ValueError) as exc:
            raise MacOSActionError() from exc
        selector = (
            "-b" if _BUNDLE_ID_PATTERN.fullmatch(safe_application) else "-a"
        )
        return await self._run(
            ("/usr/bin/open", selector, safe_application)
        )

    async def open_url(self, url: str) -> dict[str, str]:
        try:
            safe_url = normalize_public_https_url(url)
        except (TypeError, ValueError) as exc:
            raise MacOSActionError() from exc
        return await self._run(("/usr/bin/open", safe_url))

    async def toggle_media(self, player: str) -> dict[str, str]:
        try:
            script = _TOGGLE_MEDIA_SCRIPTS[player]
        except KeyError as exc:
            raise MacOSActionError() from exc
        return await self._run(("/usr/bin/osascript", "-e", script))

    async def set_volume(self, volume: int) -> dict[str, str]:
        if type(volume) is not int or not 0 <= volume <= 100:
            raise MacOSActionError()
        script = _SET_VOLUME_SCRIPT.format(volume=volume)
        return await self._run(("/usr/bin/osascript", "-e", script))

    async def _run(self, argv: tuple[str, ...]) -> dict[str, str]:
        try:
            result = await self.runner.run(argv, timeout=3)
        except Exception as exc:
            raise MacOSActionError() from exc
        if result.returncode != 0:
            raise MacOSActionError()
        return {"status": "completed"}


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
