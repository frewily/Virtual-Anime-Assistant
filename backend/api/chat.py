from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.dependencies import get_runtime
from channels.desktop import (
    desktop_chat_to_message,
    optional_client_message_id,
)
from core.runtime import AssistantRuntime
from domain.responses import ResponseKind

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    model_config = {"populate_by_name": True}

    source: Literal["desktop"]
    sender_id: str = Field(alias="senderId", min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    message_id: str | None = Field(
        default=None,
        alias="messageId",
        min_length=1,
        max_length=200,
    )

    @field_validator("sender_id", "content", mode="before")
    @classmethod
    def strip_required_text_fields(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, value):
        if value is None or not isinstance(value, str):
            return value
        message_id = optional_client_message_id(value)
        if message_id is None:
            raise ValueError(
                "messageId must be between 1 and 200 characters"
            )
        return message_id


@router.post("/chat/message")
async def handle_message(
    msg: ChatMessage,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    response = await runtime.application.handle(
        desktop_chat_to_message(
            msg.sender_id,
            msg.content,
            message_id=msg.message_id,
        )
    )
    if response.kind is ResponseKind.ERROR:
        raise HTTPException(
            status_code=503,
            detail=response.text or "助手暂时无法处理请求。",
        )
    return {"reply": response.text, "status": "ok"}
