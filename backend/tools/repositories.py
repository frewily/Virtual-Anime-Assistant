from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from domain.tools import (
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolDecision,
    ToolDecisionClaim,
    ToolRequestRecord,
    ToolRequestState,
)


@runtime_checkable
class ToolRepository(Protocol):
    async def create_request(
        self,
        record: ToolRequestRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None: ...

    async def create_confirmation(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None: ...

    async def claim_decision(
        self,
        confirmation_id: str,
        decision: ToolDecision,
        now: datetime,
    ) -> ToolDecisionClaim | None: ...

    async def transition_request(
        self,
        request_id: str,
        expected: set[ToolRequestState],
        state: ToolRequestState,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        event: ToolAuditEvent,
    ) -> ToolRequestRecord | None: ...

    async def cancel_request(
        self,
        request_id: str,
        now: datetime,
    ) -> ToolRequestRecord | None: ...

    async def get_request(
        self,
        request_id: str,
    ) -> ToolRequestRecord | None: ...

    async def get_confirmation(
        self,
        confirmation_id: str,
    ) -> ToolConfirmationRecord | None: ...

    async def get_confirmation_for_request(
        self,
        request_id: str,
    ) -> ToolConfirmationRecord | None: ...

    async def list_pending_confirmations(
        self,
        now: datetime,
    ) -> list[ToolConfirmationRecord]: ...
