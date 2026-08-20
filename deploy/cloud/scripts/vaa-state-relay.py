#!/usr/bin/env python3
"""Forward one bounded, device-bound snapshot to the loopback VAA API."""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, Protocol


MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 1024
TOKEN_PATH = Path("/etc/virtual-anime-assistant/state-relay-token")
_DEVICE_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class _Response(Protocol):
    status: int

    def read(self, size: int = -1) -> bytes: ...


class _Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[..., _Connection]


def _read_valid_token(token_path: Path) -> str:
    with token_path.open("rb") as token_file:
        raw = token_file.read(258)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw:
        raise ValueError("invalid token")
    token = raw.decode("ascii")
    if not 32 <= len(raw) <= 256:
        raise ValueError("invalid token")
    if any(character < "!" or character > "~" for character in token):
        raise ValueError("invalid token")
    return token


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def relay(
    device_id: str,
    *,
    stdin: BinaryIO,
    environ: Mapping[str, str],
    token_path: Path = TOKEN_PATH,
    connection_factory: ConnectionFactory = http.client.HTTPConnection,
) -> int:
    """Validate and forward one snapshot without exposing failure details."""

    try:
        if _DEVICE_ID_PATTERN.fullmatch(device_id) is None:
            return 2
        if environ.get("SSH_ORIGINAL_COMMAND"):
            return 2
        payload = stdin.read(MAX_SNAPSHOT_BYTES + 1)
        if not payload or len(payload) > MAX_SNAPSHOT_BYTES:
            return 2
        document = json.loads(payload, parse_constant=_reject_json_constant)
        if not isinstance(document, dict):
            return 2
        if document.get("deviceId") != device_id:
            return 2
        token = _read_valid_token(token_path)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return 2

    connection: _Connection | None = None
    try:
        connection = connection_factory("127.0.0.1", 8080, timeout=5)
        connection.request(
            "POST",
            "/api/computer/state",
            body=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES + 1)
        return 0 if 200 <= response.status < 300 else 1
    except Exception:
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else argv
    if len(arguments) != 2:
        return 2
    return relay(
        arguments[1],
        stdin=sys.stdin.buffer,
        environ=os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
