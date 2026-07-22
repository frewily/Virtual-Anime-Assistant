from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MessageStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class StoredMessage(BaseModel):
    id: str
    conversation_id: str
    correlation_id: str | None = None
    role: str
    content: str
    model: str | None = None
    status: MessageStatus = MessageStatus.COMPLETED
    created_at: datetime = Field(default_factory=utc_now)


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str
    owner_id: str
    content: str
    normalized_content: str
    source_message_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ModelCallRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    message_id: str
    model: str
    status: str
    latency_ms: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
