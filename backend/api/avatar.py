from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.dependencies import get_runtime
from api.ws import DesktopWebSocketHub
from channels.desktop import client_payload_to_message, response_to_desktop_payload
from core.runtime import AssistantRuntime

router = APIRouter(tags=["avatar"])


class AvatarActionRequest(BaseModel):
    action: str = Field(default="click", min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None


@router.get("/avatar/status")
def get_avatar_status(
    request: Request,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    del runtime
    hub: DesktopWebSocketHub = request.app.state.desktop_hub
    return {"connected": hub.has_connections(), "expression": None}


@router.post("/avatar/action")
async def perform_action(
    action: AvatarActionRequest,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    message = client_payload_to_message(
        {"type": "interaction", **action.model_dump()}
    )
    response = await runtime.application.handle(message)
    return {"status": "ok", "action": response_to_desktop_payload(response)}
