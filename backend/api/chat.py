from fastapi import APIRouter
from pydantic import BaseModel, Field
from channels.desktop import desktop_chat_to_message
from core.runtime import runtime

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    model_config = {"populate_by_name": True}

    source: str
    sender_id: str = Field(alias="senderId")
    content: str


@router.post("/chat/message")
async def handle_message(msg: ChatMessage):
    response = await runtime.application.handle(
        desktop_chat_to_message(msg.sender_id, msg.content)
    )
    return {"reply": response.text, "status": "ok"}
