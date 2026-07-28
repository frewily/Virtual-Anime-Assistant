from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.dependencies import get_runtime
from core.runtime import AssistantRuntime
from domain.tools import (
    ToolConfirmationView,
    ToolDecision,
    ToolRequest,
    ToolRequestState,
    ToolRequestView,
    ToolSource,
)
from tools.registry import ToolNotFoundError
from tools.service import ToolArgumentsError, ToolStateConflictError


router = APIRouter(tags=["tools"])


class ToolRequestBody(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
    )

    tool: str = Field(
        min_length=3,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_.-]{2,99}$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(
        default=None,
        alias="correlationId",
        min_length=1,
        max_length=200,
    )

    @field_validator("tool", "correlation_id", mode="before")
    @classmethod
    def strip_text_fields(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


class ToolDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ToolDecision


def confirmation_payload(
    confirmation: ToolConfirmationView,
) -> dict[str, Any]:
    return {
        "id": confirmation.id,
        "requestId": confirmation.request_id,
        "tool": confirmation.tool,
        "title": confirmation.title,
        "arguments": confirmation.arguments,
        "impact": confirmation.impact,
        "cancellable": confirmation.cancellable,
        "expiresAt": confirmation.expires_at.isoformat(),
    }


def request_payload(request: ToolRequestView) -> dict[str, Any]:
    return {
        "requestId": request.request_id,
        "correlationId": request.correlation_id,
        "tool": request.tool,
        "state": request.state.value,
        "result": request.result,
        "errorCode": request.error_code,
        "confirmation": (
            confirmation_payload(request.confirmation)
            if request.confirmation is not None
            else None
        ),
    }


def require_tool_service(runtime: AssistantRuntime):
    service = runtime.tool_service
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="tool service unavailable",
        )
    return service


@router.post("/tools/requests")
async def create_tool_request(
    body: ToolRequestBody,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    service = require_tool_service(runtime)
    try:
        result = await service.request(
            ToolRequest(
                correlation_id=body.correlation_id or uuid4().hex,
                source=ToolSource.DESKTOP,
                tool_name=body.tool,
                arguments=body.arguments,
            )
        )
    except ToolNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="tool not found",
        ) from exc
    except ToolArgumentsError as exc:
        raise HTTPException(
            status_code=422,
            detail="tool arguments are invalid",
        ) from exc

    status_code = (
        202
        if result.state is ToolRequestState.PENDING_CONFIRMATION
        else 200
    )
    return JSONResponse(
        status_code=status_code,
        content=request_payload(result),
    )


@router.get("/tools/confirmations")
async def list_tool_confirmations(
    runtime: AssistantRuntime = Depends(get_runtime),
):
    confirmations = await require_tool_service(
        runtime
    ).list_pending_confirmations()
    return [
        confirmation_payload(confirmation)
        for confirmation in confirmations
    ]


@router.post("/tools/confirmations/{confirmation_id}/decision")
async def decide_tool_confirmation(
    confirmation_id: str,
    body: ToolDecisionBody,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    service = require_tool_service(runtime)
    existing = await service.repository.get_confirmation(confirmation_id)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="confirmation not found",
        )
    try:
        result = await service.decide(
            confirmation_id,
            body.decision,
        )
    except ToolStateConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="confirmation state conflict",
        ) from exc
    return request_payload(result)


@router.post("/tools/requests/{request_id}/cancel")
async def cancel_tool_request(
    request_id: str,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    service = require_tool_service(runtime)
    try:
        result = await service.cancel(request_id)
    except ToolStateConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="tool request cannot be cancelled",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="tool request not found",
        )
    return request_payload(result)


@router.get("/tools/requests/{request_id}")
async def get_tool_request(
    request_id: str,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    result = await require_tool_service(runtime).get_request(request_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="tool request not found",
        )
    return request_payload(result)
