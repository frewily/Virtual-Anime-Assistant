"""Per-launch authentication for the packaged desktop runtime."""

from __future__ import annotations

import hmac
import re
from http.cookies import SimpleCookie

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


DESKTOP_TOKEN_HEADER = b"x-vaa-desktop-token"
DESKTOP_SUBPROTOCOL_PREFIX = "vaa.desktop."
DESKTOP_TOKEN_COOKIE = "vaa_desktop_runtime"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_COOKIE_AUTH_PATHS = frozenset({"/api/status/cloud"})


def _allows_cookie_auth(path: object) -> bool:
    return isinstance(path, str) and (
        path == "/api/settings"
        or path.startswith("/api/settings/")
        or path in _COOKIE_AUTH_PATHS
    )


def normalize_desktop_access_token(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid desktop access token")
    return value


def _single_header(scope: Scope, name: bytes) -> bytes | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == name]
    return values[0] if len(values) == 1 else None


def desktop_websocket_subprotocol(scope: Scope, token: str | None) -> str | None:
    if token is None:
        return None
    expected = f"{DESKTOP_SUBPROTOCOL_PREFIX}{token}"
    raw = _single_header(scope, b"sec-websocket-protocol")
    if raw is None:
        return None
    try:
        candidates = [item.strip() for item in raw.decode("ascii").split(",")]
    except UnicodeDecodeError:
        return None
    return expected if len(candidates) == 1 and hmac.compare_digest(candidates[0], expected) else None


class DesktopAccessMiddleware:
    """Require the ephemeral desktop token for local HTTP API routes."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = normalize_desktop_access_token(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.token is None
            or scope["type"] != "http"
            or not str(scope.get("path", "")).startswith("/api/")
            or str(scope.get("method", "")).upper() == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        supplied = _single_header(scope, DESKTOP_TOKEN_HEADER)
        supplied_by_header = supplied is not None
        cookie_auth_allowed = _allows_cookie_auth(scope.get("path"))
        if supplied is None and cookie_auth_allowed:
            cookie_header = _single_header(scope, b"cookie")
            if cookie_header is not None:
                try:
                    cookies = SimpleCookie(cookie_header.decode("ascii"))
                    morsel = cookies.get(DESKTOP_TOKEN_COOKIE)
                    supplied = (
                        morsel.value.encode("ascii") if morsel is not None else None
                    )
                except (UnicodeDecodeError, ValueError):
                    supplied = None
        valid = False
        if supplied is not None:
            try:
                valid = hmac.compare_digest(supplied.decode("ascii"), self.token)
            except UnicodeDecodeError:
                valid = False
        if valid:
            if not supplied_by_header:
                await self.app(scope, receive, send)
                return

            if not cookie_auth_allowed:
                await self.app(scope, receive, send)
                return

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.append(
                        "Set-Cookie",
                        f"{DESKTOP_TOKEN_COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict",
                    )
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return

        response = JSONResponse(
            status_code=401,
            content={"error": {"code": "DESKTOP_ACCESS_DENIED", "message": "access denied"}},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


__all__ = [
    "DESKTOP_SUBPROTOCOL_PREFIX",
    "DESKTOP_TOKEN_COOKIE",
    "DesktopAccessMiddleware",
    "desktop_websocket_subprotocol",
    "normalize_desktop_access_token",
]
