"""Fail-closed transport and browser security for the local settings API."""

from __future__ import annotations

import os
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


SETTINGS_SESSION_COOKIE = "vaa_settings_session"
LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Frame-Options", "DENY"),
)


def is_settings_path(path: object) -> bool:
    """Return whether a URL path is inside a settings path segment."""

    if not isinstance(path, str):
        return False
    return (
        path == "/api/settings"
        or path.startswith("/api/settings/")
        or path == "/settings"
        or path.startswith("/settings/")
    )


def _single_header(scope: Scope, name: bytes) -> bytes | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == name]
    return values[0] if len(values) == 1 else None


def add_security_headers(headers: MutableHeaders) -> None:
    for name, value in SECURITY_HEADERS:
        headers[name] = value


def access_denied_response() -> JSONResponse:
    response = JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "SETTINGS_ACCESS_DENIED",
                "message": "无法访问设置服务",
            }
        },
    )
    add_security_headers(response.headers)
    return response


class SettingsSecurityMiddleware:
    """Protect settings endpoints without trusting proxy forwarding headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        port = os.getenv("ASSISTANT_PORT", "8080").strip()
        if not port.isascii() or not port.isdigit() or not (1 <= int(port) <= 65535):
            raise ValueError("invalid settings port")
        self.settings_host = f"127.0.0.1:{port}".encode("ascii")
        self.settings_origin = f"http://127.0.0.1:{port}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket" and is_settings_path(scope.get("path")):
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http" or not is_settings_path(scope.get("path")):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_host = client[0] if client else None
        host = _single_header(scope, b"host")
        origin_ok = True
        if scope.get("method", "").upper() in STATE_CHANGING_METHODS:
            origin_ok = _single_header(scope, b"origin") == self.settings_origin
        if client_host not in LOCAL_CLIENTS or host != self.settings_host or not origin_ok:
            await access_denied_response()(scope, receive, send)
            return

        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                add_security_headers(headers)
                # Settings endpoints intentionally never participate in CORS.
                for name in tuple(headers.keys()):
                    if name.lower().startswith("access-control-"):
                        del headers[name]
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if response_started:
                raise
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "SETTINGS_INTERNAL_ERROR",
                        "message": "设置服务暂时不可用",
                    }
                },
            )
            add_security_headers(response.headers)
            await response(scope, receive, send)


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "SETTINGS_SESSION_COOKIE",
    "SettingsSecurityMiddleware",
    "add_security_headers",
    "is_settings_path",
]
