import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from domain.tools import (
    ConfirmationState,
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolConfirmationView,
    ToolDecision,
    ToolEvent,
    ToolEventType,
    ToolRequest,
    ToolRequestRecord,
    ToolRequestState,
    ToolRequestView,
    ToolRisk,
    utc_now,
)
from tools.policy import ToolPolicy, summarize_arguments
from tools.registry import ToolDefinition, ToolNotFoundError, ToolRegistry
from tools.repositories import ToolRepository


logger = logging.getLogger(__name__)
ToolEventSubscriber = Callable[[ToolEvent], Awaitable[None]]


class ToolArgumentsError(ValueError):
    pass


class ToolStateConflictError(RuntimeError):
    pass


class ToolExecutionError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        if not error_code or len(error_code) > 100:
            raise ValueError("tool error code is invalid")
        super().__init__("tool execution failed")
        self.error_code = error_code


class ToolEventPublisher:
    def __init__(self) -> None:
        self._subscribers: list[ToolEventSubscriber] = []

    def subscribe(
        self,
        subscriber: ToolEventSubscriber,
    ) -> Callable[[], None]:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    async def publish(self, event: ToolEvent) -> None:
        results = await asyncio.gather(
            *(
                subscriber(event)
                for subscriber in tuple(self._subscribers)
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "Tool event subscriber failed: %s",
                    type(result).__name__,
                )


class ToolExecutionService:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        repository: ToolRepository,
        policy: ToolPolicy | None = None,
        confirmation_timeout_seconds: float = 60,
        publisher: ToolEventPublisher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation timeout must be greater than zero")
        self.registry = registry
        self.repository = repository
        self.policy = policy or ToolPolicy()
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.publisher = publisher or ToolEventPublisher()
        self.clock = clock or utc_now
        self._pending_arguments: dict[str, BaseModel] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()

    async def request(self, request: ToolRequest) -> ToolRequestView:
        definition = self.registry.require(request.tool_name)
        validated_arguments = self._validate_arguments(
            definition,
            request.arguments,
        )
        now = self.clock()
        risk = self.policy.risk_for(definition, request.arguments)
        summary = summarize_arguments(
            validated_arguments.model_dump(mode="json"),
            definition.sensitive_fields,
        )
        state = (
            ToolRequestState.RUNNING
            if risk is ToolRisk.LOW
            else ToolRequestState.PENDING_CONFIRMATION
        )
        record = ToolRequestRecord(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            source=request.source,
            tool_name=definition.name,
            title=definition.title,
            risk=risk,
            state=state,
            arguments_summary=summary,
            impact=definition.impact,
            cancellable=definition.cancellable,
            timeout_seconds=definition.timeout_seconds,
            created_at=request.created_at,
            updated_at=now,
        )
        requested_event = self._audit(
            record.request_id,
            "requested",
            now,
            details={"arguments": summary},
        )

        if risk is ToolRisk.LOW:
            await self.repository.create_request(
                record,
                [
                    requested_event,
                    self._audit(
                        record.request_id,
                        "execution_started",
                        now,
                    ),
                ],
            )
            return await self._execute(
                record,
                definition,
                validated_arguments,
            )

        confirmation = ToolConfirmationRecord(
            request_id=record.request_id,
            requested_at=now,
            expires_at=now
            + timedelta(seconds=self.confirmation_timeout_seconds),
        )
        await self.repository.create_confirmation(
            record,
            confirmation,
            [
                requested_event,
                self._audit(
                    record.request_id,
                    "confirmation_required",
                    now,
                ),
            ],
        )
        self._pending_arguments[record.request_id] = validated_arguments
        view = self._to_view(record, confirmation)
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.CONFIRMATION_REQUIRED,
                request=view,
            )
        )
        return view

    async def decide(
        self,
        confirmation_id: str,
        decision: ToolDecision,
    ) -> ToolRequestView:
        now = self.clock()
        claim = await self.repository.claim_decision(
            confirmation_id,
            decision,
            now,
        )
        if claim is None:
            raise ToolStateConflictError("confirmation was not found")

        if not claim.claimed:
            return self._to_view(claim.request, claim.confirmation)

        if claim.request.state is not ToolRequestState.RUNNING:
            self._pending_arguments.pop(claim.request.request_id, None)
            view = self._to_view(claim.request, claim.confirmation)
            await self.publisher.publish(
                ToolEvent(
                    type=ToolEventType.CONFIRMATION_UPDATED,
                    request=view,
                )
            )
            return view

        definition = self.registry.require(claim.request.tool_name)
        validated_arguments = self._pending_arguments.pop(
            claim.request.request_id,
            None,
        )
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.CONFIRMATION_UPDATED,
                request=self._to_view(
                    claim.request,
                    claim.confirmation,
                ),
            )
        )
        if validated_arguments is None:
            failed = await self._transition(
                claim.request,
                ToolRequestState.FAILED,
                "request_context_unavailable",
            )
            return self._to_view(failed, claim.confirmation)

        started_at = self.clock()
        await self.repository.transition_request(
            claim.request.request_id,
            {ToolRequestState.RUNNING},
            ToolRequestState.RUNNING,
            event=self._audit(
                claim.request.request_id,
                "execution_started",
                started_at,
            ),
        )
        return await self._execute(
            claim.request,
            definition,
            validated_arguments,
        )

    async def cancel(
        self,
        request_id: str,
    ) -> ToolRequestView | None:
        request = await self.repository.get_request(request_id)
        if request is None:
            return None
        if (
            request.state is ToolRequestState.RUNNING
            and not request.cancellable
        ):
            raise ToolStateConflictError(
                "running tool does not support safe cancellation"
            )
        if request.state not in {
            ToolRequestState.PENDING_CONFIRMATION,
            ToolRequestState.RUNNING,
        }:
            confirmation = await self.repository.get_confirmation_for_request(
                request_id
            )
            return self._to_view(request, confirmation)

        cancelled = await self.repository.cancel_request(
            request_id,
            self.clock(),
        )
        if cancelled is None:
            return None
        self._pending_arguments.pop(request_id, None)
        running_task = self._running_tasks.get(request_id)
        if running_task is not None and not running_task.done():
            self._cancel_requested.add(request_id)
            running_task.cancel()
        confirmation = await self.repository.get_confirmation_for_request(
            request_id
        )
        view = self._to_view(cancelled, confirmation)
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.CONFIRMATION_UPDATED,
                request=view,
            )
        )
        return view

    async def get_request(
        self,
        request_id: str,
    ) -> ToolRequestView | None:
        request = await self.repository.get_request(request_id)
        if request is None:
            return None
        confirmation = await self.repository.get_confirmation_for_request(
            request_id
        )
        return self._to_view(request, confirmation)

    async def list_pending_confirmations(
        self,
    ) -> list[ToolConfirmationView]:
        confirmations = await self.repository.list_pending_confirmations(
            self.clock()
        )
        views: list[ToolConfirmationView] = []
        for confirmation in confirmations:
            request = await self.repository.get_request(
                confirmation.request_id
            )
            if request is not None:
                views.append(
                    self._confirmation_view(request, confirmation)
                )
        return views

    async def _execute(
        self,
        record: ToolRequestRecord,
        definition: ToolDefinition,
        validated_arguments: BaseModel,
    ) -> ToolRequestView:
        handler_task = asyncio.create_task(
            definition.handler(validated_arguments)
        )
        self._running_tasks[record.request_id] = handler_task
        try:
            result = await asyncio.wait_for(
                handler_task,
                timeout=definition.timeout_seconds,
            )
            if not isinstance(result, dict):
                raise TypeError("tool result must be a JSON object")
            safe_result = summarize_arguments(
                result,
                definition.sensitive_fields,
            )
            updated = await self.repository.transition_request(
                record.request_id,
                {ToolRequestState.RUNNING},
                ToolRequestState.SUCCEEDED,
                result=safe_result,
                event=self._audit(
                    record.request_id,
                    "succeeded",
                    self.clock(),
                ),
            )
            final = updated or await self.repository.get_request(
                record.request_id
            )
        except TimeoutError:
            final = await self._transition(
                record,
                ToolRequestState.FAILED,
                "execution_timeout",
            )
        except asyncio.CancelledError:
            final = await self.repository.get_request(record.request_id)
            if record.request_id not in self._cancel_requested:
                raise
        except ToolExecutionError as error:
            final = await self._transition(
                record,
                ToolRequestState.FAILED,
                error.error_code,
            )
        except Exception as error:
            logger.warning(
                "Tool execution failed: tool=%s error=%s",
                definition.name,
                type(error).__name__,
            )
            final = await self._transition(
                record,
                ToolRequestState.FAILED,
                "execution_failed",
            )
        finally:
            self._running_tasks.pop(record.request_id, None)
            self._cancel_requested.discard(record.request_id)

        if final is None:
            raise ToolStateConflictError("tool request state was lost")
        confirmation = await self.repository.get_confirmation_for_request(
            record.request_id
        )
        view = self._to_view(final, confirmation)
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.REQUEST_UPDATED,
                request=view,
            )
        )
        return view

    async def _transition(
        self,
        record: ToolRequestRecord,
        state: ToolRequestState,
        error_code: str,
    ) -> ToolRequestRecord:
        updated = await self.repository.transition_request(
            record.request_id,
            {ToolRequestState.RUNNING},
            state,
            error_code=error_code,
            event=self._audit(
                record.request_id,
                "failed" if state is ToolRequestState.FAILED else state.value,
                self.clock(),
            ),
        )
        if updated is not None:
            return updated
        existing = await self.repository.get_request(record.request_id)
        if existing is None:
            raise ToolStateConflictError("tool request was not found")
        return existing

    @staticmethod
    def _validate_arguments(
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> BaseModel:
        try:
            return definition.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolArgumentsError("tool arguments are invalid") from exc

    @staticmethod
    def _audit(
        request_id: str,
        event_type: str,
        created_at: datetime,
        *,
        details: dict[str, Any] | None = None,
    ) -> ToolAuditEvent:
        return ToolAuditEvent(
            request_id=request_id,
            event_type=event_type,
            details=details or {},
            created_at=created_at,
        )

    def _to_view(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord | None,
    ) -> ToolRequestView:
        return ToolRequestView(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            tool=request.tool_name,
            state=request.state,
            result=request.result,
            error_code=request.error_code,
            confirmation=(
                self._confirmation_view(request, confirmation)
                if confirmation is not None
                else None
            ),
        )

    @staticmethod
    def _confirmation_view(
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
    ) -> ToolConfirmationView:
        return ToolConfirmationView(
            id=confirmation.confirmation_id,
            request_id=request.request_id,
            tool=request.tool_name,
            title=request.title,
            arguments=request.arguments_summary,
            impact=request.impact,
            cancellable=request.cancellable,
            expires_at=confirmation.expires_at,
        )
