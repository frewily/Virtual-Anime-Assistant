from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ResponseKind(str, Enum):
    SPEAK = "speak"
    ACTION = "action"
    STATUS = "status"
    ERROR = "error"


class AvatarCue(BaseModel):
    emotion: str | None = Field(default=None, max_length=100)
    intent: str | None = Field(default=None, max_length=100)
    expression: str | None = Field(default=None, max_length=100)
    motion: str | None = Field(default=None, max_length=100)
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)


class AssistantResponse(BaseModel):
    response_id: str = Field(default_factory=lambda: uuid4().hex)
    correlation_id: str
    conversation_id: str
    kind: ResponseKind
    text: str | None = Field(default=None, max_length=4000)
    avatar: AvatarCue | None = None
    audio_url: str | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self):
        if self.kind is ResponseKind.SPEAK and not self.text:
            raise ValueError("speak response requires text")
        if self.kind is ResponseKind.ACTION and self.avatar is None:
            raise ValueError("action response requires avatar cue")
        return self
