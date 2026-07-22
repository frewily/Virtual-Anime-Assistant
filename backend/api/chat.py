from fastapi import APIRouter
from pydantic import BaseModel, Field
from core.runtime import runtime

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    model_config = {"populate_by_name": True}

    source: str
    sender_id: str = Field(alias="senderId")
    content: str


@router.post("/chat/message")
async def handle_message(msg: ChatMessage):
    payload = await runtime.router.handle_client_message(
        {"type": "chat", **msg.model_dump(by_alias=True)}
    )
    return {"reply": payload["text"], "status": "ok"}
