"""Report current computer state through a strictly configured SSH tunnel."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from computer.models import ComputerSnapshot


_LOGGER = logging.getLogger("computer.reporter")
_HEARTBEAT_SECONDS = 15
_MAX_BACKOFF_SECONDS = 60
_DEFAULT_PROCESS_SHUTDOWN_TIMEOUT = 5.0
_SSH_TARGET_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)


class ReporterError(RuntimeError):
    """A stable, deliberately redacted reporter failure."""


class _Process(Protocol):
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _HttpClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...

    async def aclose(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[_Process]]
SnapshotReader = Callable[[], ComputerSnapshot | None]
Sleep = Callable[[float], Awaitable[None]]


class ComputerStateReporter:
    """Own an SSH forwarding process and publish each new snapshot once."""

    def __init__(
        self,
        *,
        latest_snapshot: SnapshotReader,
        token: str,
        ssh_target: str,
        ssh_port: int,
        identity_file: Path | str,
        known_hosts_file: Path | str,
        local_port: int,
        report_path: str = "/api/computer/state",
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        http_client: _HttpClient | None = None,
        sleep: Sleep = asyncio.sleep,
        process_shutdown_timeout: float = _DEFAULT_PROCESS_SHUTDOWN_TIMEOUT,
    ) -> None:
        if not callable(latest_snapshot):
            raise TypeError("latest snapshot reader is invalid")
        if not token:
            raise ValueError("computer state report token is required")
        if _SSH_TARGET_PATTERN.fullmatch(ssh_target) is None:
            raise ValueError("computer state SSH target is invalid")
        if not 1 <= ssh_port <= 65535:
            raise ValueError("computer state SSH port is invalid")
        if not 1 <= local_port <= 65535:
            raise ValueError("computer state local port is invalid")
        if not report_path.startswith("/") or report_path.startswith("//"):
            raise ValueError("computer state report path is invalid")
        if process_shutdown_timeout <= 0:
            raise ValueError("computer state process shutdown timeout is invalid")

        self._latest_snapshot = latest_snapshot
        self._token = token
        self._ssh_target = ssh_target
        self._ssh_port = ssh_port
        self._identity_file = Path(identity_file)
        self._known_hosts_file = Path(known_hosts_file)
        self._local_port = local_port
        self._report_url = f"http://127.0.0.1:{local_port}{report_path}"
        self._process_factory = process_factory
        self._owns_http_client = http_client is None
        self._http_client = (
            httpx.AsyncClient(trust_env=False, timeout=10.0)
            if http_client is None
            else http_client
        )
        self._sleep = sleep
        self._process_shutdown_timeout = process_shutdown_timeout
        self._process: _Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._last_collected_at: datetime | None = None
        self._stop_requested = False
        self._closing = False
        self._closed = False

    @property
    def ssh_argv(self) -> tuple[str, ...]:
        """Return the fixed OpenSSH invocation without any bearer secret."""

        return (
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
            f"UserKnownHostsFile={self._known_hosts_file}",
            "-i",
            str(self._identity_file),
            "-p",
            str(self._ssh_port),
            "-L",
            f"127.0.0.1:{self._local_port}:127.0.0.1:8080",
            self._ssh_target,
        )

    async def open_tunnel(self) -> None:
        """Start the tunnel if it is not already alive."""

        async with self._lifecycle_lock:
            if self._closing or self._closed:
                raise ReporterError("computer state reporter is closed")
            if self._process is not None and self._process.returncode is None:
                return
            if self._process is not None:
                if not await self._bounded_wait(self._process):
                    _LOGGER.warning("computer state SSH tunnel reap failed")
                    raise ReporterError("computer state SSH tunnel failed")
                self._process = None
            try:
                self._process = await self._process_factory(
                    *self.ssh_argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning("computer state SSH tunnel failed")
                raise ReporterError("computer state SSH tunnel failed") from None

    async def report_once(self) -> bool:
        """Publish the latest not-yet-acknowledged snapshot, if one exists."""

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
            collected_at = snapshot.collected_at
            response = await self._http_client.post(
                self._report_url,
                json=snapshot.model_dump(mode="json", by_alias=True),
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("computer state report failed")
            raise ReporterError("computer state report failed") from None

        self._last_collected_at = collected_at
        return True

    async def run(self) -> None:
        """Maintain the tunnel and report with heartbeat/backoff scheduling."""

        if self._closing or self._closed:
            raise ReporterError("computer state reporter is closed")
        self._stop_requested = False
        failure_count = 0
        while not self._stop_requested:
            try:
                await self.open_tunnel()
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
        """Stop reporting, terminate and reap SSH, then close HTTP."""

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
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.warning("computer state reporter task close failed")
        self._task = None

        try:
            async with self._lifecycle_lock:
                process = self._process
                if process is not None and await self._shutdown_process(process):
                    if self._process is process:
                        self._process = None
        finally:
            if self._owns_http_client:
                try:
                    await self._http_client.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.warning("computer state reporter HTTP close failed")
            self._closed = True

    async def _shutdown_process(self, process: _Process) -> bool:
        if process.returncode is None:
            try:
                process.terminate()
            except Exception:
                _LOGGER.warning("computer state SSH tunnel close failed")
        if await self._bounded_wait(process):
            return True

        try:
            process.kill()
        except Exception:
            _LOGGER.warning("computer state SSH tunnel kill failed")
        if await self._bounded_wait(process):
            return True
        _LOGGER.warning("computer state SSH tunnel reap failed")
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
