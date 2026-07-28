from enum import Enum

from pydantic import BaseModel, Field


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelMessage(BaseModel):
    role: ModelRole
    content: str = Field(min_length=1, max_length=12000)


class ModelRequest(BaseModel):
    correlation_id: str = Field(min_length=1, max_length=200)
    messages: list[ModelMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=8192)


class ModelReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    model: str = Field(min_length=1, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=100)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=300)
