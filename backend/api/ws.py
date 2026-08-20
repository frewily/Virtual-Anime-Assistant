import json
from collections.abc import Awaitable, Callable, Iterable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from channels.desktop import client_payload_to_message, response_to_desktop_payload
from domain.responses import AssistantResponse

router = APIRouter()

_ALLOWED_ORIGINS = {None, "null", "file://"}
LastDisconnectHandler = Callable[[], Awaitable[None]]


class DesktopWebSocketHub:
    def __init__(
        self,
        on_last_disconnect: LastDisconnectHandler | None = None,
    ) -> None:
        self._sessions: set[WebSocket] = set()
        self._on_last_disconnect = on_last_disconnect

    @property
    def connected_count(self) -> int:
        return len(self._sessions)

    def has_connections(self) -> bool:
        return bool(self._sessions)

    def attach(self, websocket: WebSocket) -> None:
        self._sessions.add(websocket)

    async def detach(self, websocket: WebSocket) -> None:
        await self._detach_many((websocket,))

    async def broadcast_json(self, payload: dict) -> None:
        message = json.dumps(payload, ensure_ascii=False)
        disconnected: list[WebSocket] = []
        for websocket in tuple(self._sessions):
            try:
                await websocket.send_text(message)
            except (RuntimeError, WebSocketDisconnect):
                disconnected.append(websocket)
        await self._detach_many(disconnected)

    async def broadcast_response(self, response: AssistantResponse) -> None:
        await self.broadcast_json(response_to_desktop_payload(response))

    async def _detach_many(
        self,
        websockets: Iterable[WebSocket],
    ) -> None:
        had_connections = bool(self._sessions)
        self._sessions.difference_update(websockets)
        if (
            had_connections
            and not self._sessions
            and self._on_last_disconnect is not None
        ):
            await self._on_last_disconnect()


def is_allowed_origin(origin: str | None) -> bool:
    return origin in _ALLOWED_ORIGINS


def parse_client_message(raw_message: str) -> dict:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise ValueError("message must be valid JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message


async def notify_confirmation_client_disconnected(runtime) -> None:
    if runtime is None:
        return
    service = getattr(runtime, "tool_service", None)
    disconnected = getattr(
        service,
        "confirmation_client_disconnected",
        None,
    )
    if disconnected is not None:
        await disconnected()


@router.websocket("/ws/avatar")
async def avatar_websocket(ws: WebSocket):
    if not is_allowed_origin(ws.headers.get("origin")):
        await ws.close(code=1008, reason="origin not allowed")
        return

    hub: DesktopWebSocketHub = ws.app.state.desktop_hub
    await ws.accept()
    hub.attach(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                payload = parse_client_message(data)
                runtime = ws.app.state.runtime
                await runtime.application.handle(client_payload_to_message(payload))
            except (ValueError, TypeError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.detach(ws)
