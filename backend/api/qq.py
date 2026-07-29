"""OneBot 11 reverse WebSocket and safe channel status."""

import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from api.dependencies import get_runtime
from channels.onebot.connection import authenticate_onebot
from channels.onebot.models import (
    ONEBOT_INVALID_EVENT,
    QQ_DISABLED,
    QQ_MISCONFIGURED,
    OneBotChannelError,
    QQState,
)
from core.runtime import AssistantRuntime


logger = logging.getLogger(__name__)
status_router = APIRouter(tags=["qq"])
websocket_router = APIRouter()


@status_router.get("/qq/status")
def get_qq_status(
    runtime: AssistantRuntime = Depends(get_runtime),
) -> dict[str, object]:
    settings = runtime.qq_settings
    if settings.configuration_error is not None:
        state = QQState.MISCONFIGURED
    elif not settings.enabled:
        state = QQState.DISABLED
    elif runtime.qq_connection.connected:
        state = QQState.CONNECTED
    else:
        state = QQState.DISCONNECTED
    return {
        "enabled": settings.enabled,
        "state": state.value,
        "allowedGroupCount": len(settings.allowed_group_ids),
        "allowedUserCount": len(settings.allowed_user_ids),
    }


@websocket_router.websocket("/ws/qq")
async def qq_websocket(ws: WebSocket) -> None:
    runtime = ws.app.state.runtime
    settings = runtime.qq_settings
    if settings.configuration_error is not None:
        await ws.close(code=1008, reason=QQ_MISCONFIGURED)
        return
    if not settings.enabled:
        await ws.close(code=1008, reason=QQ_DISABLED)
        return

    try:
        self_id = authenticate_onebot(
            ws.headers.get("authorization"),
            ws.headers.get("x-self-id"),
            settings,
        )
    except OneBotChannelError as exc:
        await ws.close(code=1008, reason=exc.code)
        return

    try:
        await runtime.qq_connection.attach(ws, self_id)
    except OneBotChannelError as exc:
        await ws.close(code=1008, reason=exc.code)
        return

    invalid_frames = 0
    try:
        await ws.accept()
        while True:
            raw_message = await ws.receive_text()
            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                invalid_frames += 1
                logger.warning(
                    "OneBot frame rejected: %s",
                    ONEBOT_INVALID_EVENT,
                )
                if invalid_frames >= 3:
                    await ws.close(
                        code=1003,
                        reason=ONEBOT_INVALID_EVENT,
                    )
                    return
                continue

            invalid_frames = 0
            if runtime.qq_connection.resolve_action_response(payload):
                continue
            try:
                await runtime.qq_channel.handle_event(
                    payload,
                    self_id=self_id,
                )
            except Exception as exc:
                logger.error(
                    "OneBot event failed: %s",
                    type(exc).__name__,
                )
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.qq_connection.detach(ws)
