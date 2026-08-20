"""Report current computer state through one-shot restricted SSH stdin."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from computer.models import ComputerSnapshot


_LOGGER = logging.getLogger("computer.reporter")
_HEARTBEAT_SECONDS = 15
_MAX_BACKOFF_SECONDS = 60
_MAX_SNAPSHOT_BYTES = 32 * 1024
_DEFAULT_PROCESS_TIMEOUT = 10.0
_DEFAULT_PROCESS_SHUTDOWN_TIMEOUT = 5.0
_SSH_TARGET_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)


class ReporterError(RuntimeError):
    """A stable, deliberately redacted reporter failure."""


class _Process(Protocol):
    returncode: int | None

    async def communicate(
        self,
        input: bytes | None = None,
    ) -> tuple[bytes | None, bytes | None]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., Awaitable[_Process]]
SnapshotReader = Callable[[], ComputerSnapshot | None]
Sleep = Callable[[float], Awaitable[None]]


def _serialize_snapshot(snapshot: ComputerSnapshot) -> bytes:
    payload = json.dumps(
        snapshot.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise ReporterError("computer state report failed")
    return payload


class ComputerStateReporter:
    """Send each new safe snapshot through one bounded SSH session."""

    def __init__(
        self,
        *,
        latest_snapshot: SnapshotReader,
        ssh_target: str,
        ssh_port: int,
        identity_file: Path | str,
        known_hosts_file: Path | str,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        sleep: Sleep = asyncio.sleep,
        process_timeout: float = _DEFAULT_PROCESS_TIMEOUT,
        process_shutdown_timeout: float = _DEFAULT_PROCESS_SHUTDOWN_TIMEOUT,
    ) -> None:
        if not callable(latest_snapshot):
            raise TypeError("latest snapshot reader is invalid")
        if _SSH_TARGET_PATTERN.fullmatch(ssh_target) is None:
            raise ValueError("computer state SSH target is invalid")
        if not 1 <= ssh_port <= 65535:
            raise ValueError("computer state SSH port is invalid")
        if process_timeout <= 0:
            raise ValueError("computer state process timeout is invalid")
        if process_shutdown_timeout <= 0:
            raise ValueError("computer state process shutdown timeout is invalid")

        self._latest_snapshot = latest_snapshot
        self._ssh_target = ssh_target
        self._ssh_port = ssh_port
        self._identity_file = Path(identity_file)
        self._known_hosts_file = Path(known_hosts_file)
        self._process_factory = process_factory
        self._sleep = sleep
        self._process_timeout = process_timeout
        self._process_shutdown_timeout = process_shutdown_timeout
        self._process: _Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._report_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._last_collected_at: datetime | None = None
        self._stop_requested = False
        self._closing = False
        self._closed = False

    @property
    def ssh_argv(self) -> tuple[str, ...]:
        """Return the fixed OpenSSH invocation without forwarding."""

        return (
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
            f"UserKnownHostsFile={self._known_hosts_file}",
            "-i",
            str(self._identity_file),
            "-p",
            str(self._ssh_port),
            self._ssh_target,
        )

    async def report_once(self) -> bool:
        """Send the latest not-yet-acknowledged snapshot, if one exists."""

        async with self._report_lock:
            if self._closing or self._closed:
                raise ReporterError("computer state reporter is closed")
            try:
                snapshot = self._latest_snapshot()
                if snapshot is None:
                    return False
                if (
                    self._last_collected_at is not None
                    and snapshot.collected_at <= self._last_collected_at
                ):
                    return False
                payload = _serialize_snapshot(snapshot)
                await self._send_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning("computer state report failed")
                raise ReporterError("computer state report failed") from None

            self._last_collected_at = snapshot.collected_at
            return True

    async def _send_payload(self, payload: bytes) -> None:
        process: _Process | None = None
        try:
            async with self._lifecycle_lock:
                if self._closing or self._closed:
                    raise ReporterError("computer state reporter is closed")
                process = await self._process_factory(
                    *self.ssh_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self._process = process

            await asyncio.wait_for(
                process.communicate(payload),
                timeout=self._process_timeout,
            )
            if process.returncode != 0:
                raise ReporterError("computer state report failed")
        except asyncio.CancelledError:
            if process is not None:
                await self._finish_cleanup(process, force=True)
            raise
        except Exception:
            if process is not None:
                await self._finish_cleanup(process, force=True)
            raise
        else:
            await self._clear_reaped_process(process)

    async def _finish_cleanup(self, process: _Process, *, force: bool) -> None:
        cleanup = asyncio.create_task(
            self._cleanup_process(process, force=force),
            name="computer-state-ssh-cleanup",
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _cleanup_process(self, process: _Process, *, force: bool) -> None:
        async with self._lifecycle_lock:
            if self._process is not process:
                return
            reaped = True
            if force or process.returncode is None:
                reaped = await self._shutdown_process(process)
            if reaped and self._process is process:
                self._process = None

    async def _clear_reaped_process(self, process: _Process) -> None:
        async with self._lifecycle_lock:
            if self._process is process:
                self._process = None

    async def run(self) -> None:
        """Report with heartbeat and bounded exponential backoff."""

        if self._closing or self._closed:
            raise ReporterError("computer state reporter is closed")
        self._stop_requested = False
        failure_count = 0
        while not self._stop_requested:
            try:
                await self.report_once()
            except ReporterError:
                failure_count += 1
                delay = min(2 ** (failure_count - 1), _MAX_BACKOFF_SECONDS)
            else:
                failure_count = 0
                delay = _HEARTBEAT_SECONDS
            await self._sleep(delay)

    def start(self) -> bool:
        """Start the reporting loop once."""

        if self._closing or self._closed:
            raise ReporterError("computer state reporter is closed")
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(
            self.run(),
            name="computer-state-reporter",
        )
        return True

    def stop(self) -> None:
        """Ask a directly awaited run loop to stop after its current sleep."""

        self._stop_requested = True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def aclose(self) -> None:
        """Stop reporting and finish all active SSH cleanup."""

        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(
                self._close_impl(),
                name="computer-state-reporter-close",
            )
        close_task = self._close_task
        cancelled = False
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                cancelled = True
        close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_impl(self) -> None:
        self._stop_requested = True
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.warning("computer state reporter task close failed")
        self._task = None

        async with self._lifecycle_lock:
            process = self._process
        if process is not None:
            await self._cleanup_process(process, force=True)
        self._closed = True

    async def _shutdown_process(self, process: _Process) -> bool:
        if process.returncode is None:
            try:
                process.terminate()
            except Exception:
                _LOGGER.warning("computer state SSH close failed")
        if await self._bounded_wait(process):
            return True

        try:
            process.kill()
        except Exception:
            _LOGGER.warning("computer state SSH kill failed")
        if await self._bounded_wait(process):
            return True
        _LOGGER.warning("computer state SSH reap failed")
        return False

    async def _bounded_wait(self, process: _Process) -> bool:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_shutdown_timeout,
            )
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True
