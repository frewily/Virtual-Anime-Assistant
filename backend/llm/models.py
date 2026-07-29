from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_.-]{2,99}$"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelToolDefinition(_FrozenModel):
    name: str = Field(pattern=_SAFE_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def require_object_schema(self) -> "ModelToolDefinition":
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be a top-level object schema")
        return self


class ModelToolCall(_FrozenModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(pattern=_SAFE_NAME_PATTERN)
    arguments: dict[str, Any]


class ModelToolResult(_FrozenModel):
    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(pattern=_SAFE_NAME_PATTERN)
    state: str = Field(min_length=1, max_length=100)
    result: Any | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=100)


class ModelMessage(_FrozenModel):
    role: ModelRole
    content: str | None = Field(default=None, min_length=1, max_length=12000)
    tool_calls: list[ModelToolCall] = Field(default_factory=list, max_length=4)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, pattern=_SAFE_NAME_PATTERN)

    @model_validator(mode="after")
    def require_role_shape(self) -> "ModelMessage":
        if self.role in (ModelRole.SYSTEM, ModelRole.USER):
            if self.content is None:
                raise ValueError("system and user messages require content")
            if self.tool_calls or self.tool_call_id is not None or self.name is not None:
                raise ValueError("system and user messages cannot carry tool fields")
        elif self.role is ModelRole.ASSISTANT:
            if self.content is None and not self.tool_calls:
                raise ValueError("assistant messages require content or tool calls")
            if self.tool_call_id is not None or self.name is not None:
                raise ValueError("assistant messages cannot be tool results")
        elif self.role is ModelRole.TOOL:
            if (
                self.content is None
                or self.tool_call_id is None
                or self.name is None
                or self.tool_calls
            ):
                raise ValueError(
                    "tool messages require content, tool_call_id, and name only"
                )
        return self


class ModelRequest(_FrozenModel):
    correlation_id: str = Field(min_length=1, max_length=200)
    messages: list[ModelMessage] = Field(min_length=1)
    tools: list[ModelToolDefinition] = Field(default_factory=list, max_length=32)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=8192)


class ModelReply(_FrozenModel):
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    tool_calls: list[ModelToolCall] = Field(default_factory=list, max_length=4)
    model: str = Field(min_length=1, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=100)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def require_output(self) -> "ModelReply":
        if self.text is None and not self.tool_calls:
            raise ValueError("model reply requires text or tool calls")
        return self


class ModelAttempt(_FrozenModel):
    model: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=100)
    latency_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=300)


class ModelOrchestrationResult(_FrozenModel):
    reply: ModelReply
    attempts: list[ModelAttempt] = Field(min_length=1)
