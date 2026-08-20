from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from domain.messages import MessageSource


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolRisk(str, Enum):
    LOW = "low"
    HIGH = "high"


class ToolRequestState(str, Enum):
    CREATED = "created"
    PENDING_CONFIRMATION = "pending_confirmation"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ConfirmationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ToolSource(str, Enum):
    DESKTOP = "desktop"
    MODEL = "model"
    QQ = "qq"
    SYSTEM = "system"


class ToolEventType(str, Enum):
    CONFIRMATION_REQUIRED = "tool_confirmation_required"
    CONFIRMATION_UPDATED = "tool_confirmation_updated"
    REQUEST_UPDATED = "tool_request_updated"


class StrictToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def default_trusted_origin(cls, value):
        if not isinstance(value, dict) or "origin" in value:
            return value
        source = value.get("source")
        origins = {
            ToolSource.DESKTOP: MessageSource.DESKTOP,
            ToolSource.QQ: MessageSource.QQ,
            ToolSource.SYSTEM: MessageSource.SYSTEM,
            ToolSource.MODEL: MessageSource.SYSTEM,
        }
        if isinstance(source, ToolSource):
            return {**value, "origin": origins[source]}
        return value

    @field_validator("*", mode="after")
    @classmethod
    def require_aware_datetimes(cls, value):
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("datetime must include timezone information")
        return value


class ToolRequest(StrictToolModel):
    request_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )
    correlation_id: str = Field(min_length=1, max_length=200)
    source: ToolSource
    origin: MessageSource = MessageSource.SYSTEM
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ToolRequestRecord(StrictToolModel):
    request_id: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    source: ToolSource
    origin: MessageSource = MessageSource.SYSTEM
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    title: str = Field(min_length=1, max_length=200)
    risk: ToolRisk
    state: ToolRequestState
    arguments_summary: dict[str, Any] = Field(default_factory=dict)
    impact: str = Field(min_length=1, max_length=1000)
    cancellable: bool
    timeout_seconds: float = Field(gt=0, le=300)
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ToolConfirmationRecord(StrictToolModel):
    confirmation_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )
    request_id: str = Field(min_length=1, max_length=200)
    state: ConfirmationState = ConfirmationState.PENDING
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    decided_at: datetime | None = None


class ToolDecisionClaim(StrictToolModel):
    request: ToolRequestRecord
    confirmation: ToolConfirmationRecord
    claimed: bool


class ToolAuditEvent(StrictToolModel):
    event_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=1,
        max_length=200,
    )
    request_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ToolConfirmationView(StrictToolModel):
    id: str
    request_id: str
    tool: str
    title: str
    arguments: dict[str, Any]
    impact: str
    cancellable: bool
    expires_at: datetime


class ToolRequestView(StrictToolModel):
    request_id: str
    correlation_id: str
    tool: str
    state: ToolRequestState
    result: dict[str, Any] | None = None
    error_code: str | None = None
    confirmation: ToolConfirmationView | None = None


class ToolEvent(StrictToolModel):
    type: ToolEventType
    request: ToolRequestView


class ToolExecutionResult(StrictToolModel):
    request_id: str
    state: ToolRequestState
    result: dict[str, Any] | None = None
    error_code: str | None = None
