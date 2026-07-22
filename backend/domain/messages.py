from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class MessageSource(str, Enum):
    DESKTOP = "desktop"
    QQ = "qq"
    SCENARIO = "scenario"
    SYSTEM = "system"


class SenderIdentity(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)


class ChatContent(BaseModel):
    type: Literal["chat"] = "chat"
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("chat text must not be blank")
        return value


class InteractionContent(BaseModel):
    type: Literal["interaction"] = "interaction"
    action: str = Field(min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None


class ScenarioContent(BaseModel):
    type: Literal["scenario"] = "scenario"
    scenario_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    expression: str | None = Field(default=None, max_length=100)
    motion: str | None = Field(default=None, max_length=100)


MessageContent = Annotated[
    ChatContent | InteractionContent | ScenarioContent,
    Field(discriminator="type"),
]


class IncomingMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    conversation_id: str = Field(min_length=1, max_length=200)
    source: MessageSource
    sender: SenderIdentity
    content: MessageContent
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
