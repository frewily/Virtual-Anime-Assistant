import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from channels.desktop import client_payload_to_message, response_to_desktop_payload
from core.runtime import runtime
from domain.responses import AssistantResponse

router = APIRouter()

_sessions: set[WebSocket] = set()
_ALLOWED_ORIGINS = {None, "null", "file://"}


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


async def broadcast_to_desktop(response: AssistantResponse) -> None:
    message = json.dumps(response_to_desktop_payload(response), ensure_ascii=False)
    disconnected: list[WebSocket] = []
    for ws in tuple(_sessions):
        try:
            await ws.send_text(message)
        except (RuntimeError, WebSocketDisconnect):
            disconnected.append(ws)
    for ws in disconnected:
        _sessions.discard(ws)


def connected_count() -> int:
    return len(_sessions)


@router.websocket("/ws/avatar")
async def avatar_websocket(ws: WebSocket):
    if not is_allowed_origin(ws.headers.get("origin")):
        await ws.close(code=1008, reason="origin not allowed")
        return

    await ws.accept()
    _sessions.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                payload = parse_client_message(data)
                await runtime.application.handle(client_payload_to_message(payload))
            except (ValueError, TypeError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        _sessions.discard(ws)
