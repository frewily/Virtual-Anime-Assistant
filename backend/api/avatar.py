from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.ws import connected_count
from channels.desktop import client_payload_to_message, response_to_desktop_payload
from core.runtime import runtime

router = APIRouter(tags=["avatar"])


class AvatarActionRequest(BaseModel):
    action: str = Field(default="click", min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None


@router.get("/avatar/status")
def get_avatar_status():
    return {"connected": connected_count() > 0, "expression": None}


@router.post("/avatar/action")
async def perform_action(action: AvatarActionRequest):
    message = client_payload_to_message(
        {"type": "interaction", **action.model_dump()}
    )
    response = await runtime.application.handle(message)
    return {"status": "ok", "action": response_to_desktop_payload(response)}
