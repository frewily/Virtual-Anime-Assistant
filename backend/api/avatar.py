from fastapi import APIRouter
from core.runtime import runtime
from api.ws import connected_count

router = APIRouter(tags=["avatar"])


@router.get("/avatar/status")
def get_avatar_status():
    return {"connected": connected_count() > 0, "expression": None}


@router.post("/avatar/action")
async def perform_action(action: dict):
    payload = await runtime.router.handle_client_message({"type": "interaction", **action})
    return {"status": "ok", "action": payload}
