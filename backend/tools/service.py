import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from computer.models import ComputerPlatform
from domain.messages import MessageSource
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
    ToolSource,
    utc_now,
)
from tools.catalog import (
    ModelToolCallContext,
    ModelToolPolicy,
    reject_additional_arguments,
)
from tools.policy import ToolPolicy, summarize_arguments
from tools.registry import ToolDefinition, ToolNotFoundError, ToolRegistry
from tools.repositories import ToolRepository


logger = logging.getLogger(__name__)
ToolEventSubscriber = Callable[[ToolEvent], Awaitable[None]]


@dataclass(frozen=True)
class _DeferredToolUpdate:
    view: ToolRequestView
    event_type: ToolEventType
    resolve_terminal: bool = False


@dataclass(frozen=True)
class _PreparedDecision:
    view: ToolRequestView | None = None
    execution: tuple[
        asyncio.Task[ToolRequestView],
        asyncio.Event,
    ] | None = None
    update: _DeferredToolUpdate | None = None


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
        confirmation_client_online: Callable[[], bool] | None = None,
        platform: ComputerPlatform | None = None,
        runtime_profile: Literal["desktop", "cloud"] | None = None,
        allowed_model_tool_names: frozenset[str] | None = None,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation timeout must be greater than zero")
        if (
            confirmation_client_online is not None
            and not callable(confirmation_client_online)
        ):
            raise TypeError("confirmation client status must be callable")
        if platform is not None and not isinstance(
            platform,
            ComputerPlatform,
        ):
            raise TypeError("tool execution platform must be a ComputerPlatform")
        if runtime_profile not in {None, "desktop", "cloud"}:
            raise ValueError("tool execution runtime profile is invalid")
        self.registry = registry
        self.repository = repository
        self.policy = policy or ToolPolicy()
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.publisher = publisher or ToolEventPublisher()
        self.clock = clock or utc_now
        self.confirmation_client_online = (
            confirmation_client_online or (lambda: False)
        )
        self.platform = platform
        self.runtime_profile = runtime_profile
        self.allowed_model_tool_names = allowed_model_tool_names
        self.model_policy = ModelToolPolicy(
            platform=platform,
            runtime_profile=runtime_profile,
            confirmation_client_online=self.confirmation_client_online,
            allowed_tool_names=allowed_model_tool_names,
        )
        self._pending_arguments: dict[str, BaseModel] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self._pending_model_confirmations: set[str] = set()
        self._confirmation_generation = 0
        self._confirmation_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._terminal_waiters: dict[
            str,
            set[asyncio.Future[ToolRequestView]],
        ] = {}
        self._closing_task: asyncio.Task[None] | None = None
        self._unsubscribed_terminal_events = False
        self._closed = False
        self._unsubscribe_terminal_events = self.publisher.subscribe(
            self._receive_terminal_event
        )

    async def request(
        self,
        request: ToolRequest,
        *,
        model_context: ModelToolCallContext | None = None,
    ) -> ToolRequestView:
        if self._closed or self._closing_task is not None:
            raise ToolNotFoundError(request.tool_name)
        definition = self.registry.require(request.tool_name)
        source = request.source
        if not isinstance(source, ToolSource):
            raise ToolNotFoundError(request.tool_name)
        risk = self.policy.risk_for(definition, request.arguments)
        confirmation_generation = self._confirmation_generation
        if source is ToolSource.MODEL:
            if (
                not isinstance(model_context, ModelToolCallContext)
                or definition.name not in model_context.advertised_tool_names
                or not self.model_policy.allows(
                    definition,
                    model_context.channel,
                    risk,
                )
            ):
                raise ToolNotFoundError(request.tool_name)
            origin = model_context.channel
        else:
            if (
                not isinstance(request.origin, MessageSource)
                or source not in definition.allowed_sources
            ):
                raise ToolNotFoundError(request.tool_name)
            origin = request.origin
        validated_arguments = self._validate_arguments(
            definition,
            request.arguments,
        )
        now = self.clock()
        summary = self._summarize_confirmation(
            definition,
            validated_arguments,
        )
        state = (
            ToolRequestState.RUNNING
            if risk is ToolRisk.LOW
            else ToolRequestState.PENDING_CONFIRMATION
        )
        record = ToolRequestRecord(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            source=source,
            origin=origin,
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
        if source is ToolSource.MODEL:
            self._pending_model_confirmations.add(record.request_id)
            if (
                confirmation_generation != self._confirmation_generation
                or not self._confirmation_is_online()
            ):
                return await self._fail_pending_model_confirmation(
                    record.request_id
                )
        view = self._to_view(record, confirmation)
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.CONFIRMATION_REQUIRED,
                request=view,
            )
        )
        if source is ToolSource.MODEL:
            current = await self.get_request(record.request_id)
            if current is not None and self._is_terminal(current.state):
                return current
        return view

    async def decide(
        self,
        confirmation_id: str,
        decision: ToolDecision,
    ) -> ToolRequestView:
        async with self._confirmation_lock:
            async with self._execution_lock:
                prepared = await self._prepare_decision(
                    confirmation_id,
                    decision,
                )
        if prepared.execution is None:
            if prepared.update is not None:
                await self._publish_deferred_update(prepared.update)
            if prepared.view is None:
                raise ToolStateConflictError("tool decision result was lost")
            return prepared.view
        supervisor, publish_after = prepared.execution
        try:
            if prepared.update is not None:
                await self._publish_deferred_update(prepared.update)
        finally:
            publish_after.set()
        return await asyncio.shield(supervisor)

    async def _prepare_decision(
        self,
        confirmation_id: str,
        decision: ToolDecision,
    ) -> _PreparedDecision:
        now = self.clock()
        claim = await self.repository.claim_decision(
            confirmation_id,
            decision,
            now,
        )
        if claim is None:
            raise ToolStateConflictError("confirmation was not found")

        if not claim.claimed:
            self._pending_model_confirmations.discard(
                claim.request.request_id
            )
            return _PreparedDecision(
                view=self._to_view(claim.request, claim.confirmation)
            )

        if claim.request.state is not ToolRequestState.RUNNING:
            self._pending_arguments.pop(claim.request.request_id, None)
            self._pending_model_confirmations.discard(
                claim.request.request_id
            )
            view = self._to_view(claim.request, claim.confirmation)
            return _PreparedDecision(
                view=view,
                update=_DeferredToolUpdate(
                    view=view,
                    event_type=ToolEventType.CONFIRMATION_UPDATED,
                    resolve_terminal=True,
                ),
            )

        definition = self.registry.require(claim.request.tool_name)
        if (
            claim.request.source is ToolSource.MODEL
            and not self._confirmation_is_online()
        ):
            update = await self._fail_pending_model_confirmation_locked(
                claim.request.request_id,
            )
            return _PreparedDecision(view=update.view, update=update)
        validated_arguments = self._pending_arguments.get(
            claim.request.request_id,
        )
        if claim.request.source is ToolSource.MODEL:
            current = await self.get_request(claim.request.request_id)
            if current is not None and self._is_terminal(current.state):
                return _PreparedDecision(view=current)
            if not self._confirmation_is_online():
                update = await self._fail_pending_model_confirmation_locked(
                    claim.request.request_id,
                )
                return _PreparedDecision(view=update.view, update=update)
        if validated_arguments is None:
            self._pending_model_confirmations.discard(
                claim.request.request_id
            )
            failed = await self._transition(
                claim.request,
                ToolRequestState.FAILED,
                "request_context_unavailable",
            )
            view = self._to_view(failed, claim.confirmation)
            return _PreparedDecision(
                view=view,
                update=_DeferredToolUpdate(
                    view=view,
                    event_type=ToolEventType.CONFIRMATION_UPDATED,
                    resolve_terminal=True,
                ),
            )

        started_at = self.clock()
        execution_claim = await self.repository.transition_request(
            claim.request.request_id,
            {ToolRequestState.RUNNING},
            ToolRequestState.RUNNING,
            event=self._audit(
                claim.request.request_id,
                "execution_started",
                started_at,
            ),
        )
        if execution_claim is None:
            current = await self.get_request(claim.request.request_id)
            if current is None:
                raise ToolStateConflictError("tool request was not found")
            if self._is_terminal(current.state):
                self._pending_model_confirmations.discard(
                    claim.request.request_id
                )
                self._pending_arguments.pop(claim.request.request_id, None)
                return _PreparedDecision(
                    view=current,
                    update=_DeferredToolUpdate(
                        view=current,
                        event_type=ToolEventType.CONFIRMATION_UPDATED,
                        resolve_terminal=True,
                    ),
                )
            raise ToolStateConflictError("tool execution could not be claimed")

        if (
            claim.request.source is ToolSource.MODEL
            and not self._confirmation_is_online()
        ):
            update = await self._fail_pending_model_confirmation_locked(
                claim.request.request_id,
            )
            return _PreparedDecision(view=update.view, update=update)

        if claim.request.source is not ToolSource.MODEL:
            self._pending_arguments.pop(claim.request.request_id, None)
        publish_after = asyncio.Event()
        supervisor = asyncio.create_task(
            self._supervise_execution(
                execution_claim,
                definition,
                validated_arguments,
                publish_after=publish_after,
                requires_live_confirmation=(
                    claim.request.source is ToolSource.MODEL
                ),
            )
        )
        self._register_running_task(
            claim.request.request_id,
            supervisor,
        )
        running_view = self._to_view(execution_claim, claim.confirmation)
        return _PreparedDecision(
            execution=(supervisor, publish_after),
            update=_DeferredToolUpdate(
                view=running_view,
                event_type=ToolEventType.CONFIRMATION_UPDATED,
            ),
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
        self._pending_model_confirmations.discard(request_id)
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
        self._resolve_terminal_waiters(view)
        return view

    async def wait_for_terminal(
        self,
        request_id: str,
        timeout: float = 60,
    ) -> ToolRequestView:
        if timeout <= 0:
            raise ValueError("terminal wait timeout must be greater than zero")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ToolRequestView] = loop.create_future()
        waiters = self._terminal_waiters.setdefault(request_id, set())
        waiters.add(waiter)
        try:
            confirmation_deadline = loop.time() + timeout
            while True:
                current = await self.get_request(request_id)
                if current is None:
                    raise ToolStateConflictError("tool request was not found")
                if self._is_terminal(current.state):
                    return current
                if current.state is ToolRequestState.PENDING_CONFIRMATION:
                    remaining = confirmation_deadline - loop.time()
                    if remaining <= 0:
                        current = await self._expire_waiting_request(request_id)
                    else:
                        try:
                            return await asyncio.wait_for(
                                asyncio.shield(waiter),
                                timeout=remaining,
                            )
                        except TimeoutError:
                            current = await self._expire_waiting_request(
                                request_id
                            )
                    if self._is_terminal(current.state):
                        return current
                    continue
                if current.state is ToolRequestState.RUNNING:
                    record = await self.repository.get_request(request_id)
                    if record is None:
                        raise ToolStateConflictError(
                            "tool request was not found"
                        )
                    elapsed = max(
                        0.0,
                        (self.clock() - record.updated_at).total_seconds(),
                    )
                    execution_remaining = max(
                        0.0,
                        record.timeout_seconds - elapsed,
                    )
                    if execution_remaining > 0:
                        try:
                            return await asyncio.wait_for(
                                asyncio.shield(waiter),
                                timeout=execution_remaining,
                            )
                        except TimeoutError:
                            pass
                    return await self._fail_running_wait(request_id)
                raise ToolStateConflictError("tool request state is invalid")
        finally:
            waiters.discard(waiter)
            if not waiters:
                self._terminal_waiters.pop(request_id, None)

    async def aclose(self) -> None:
        if self._closed:
            return
        closing = self._closing_task
        if closing is None:
            closing = asyncio.create_task(self._close_impl())
            self._closing_task = closing
        try:
            await asyncio.shield(closing)
        except BaseException:
            if self._closing_task is closing and closing.done():
                self._closing_task = None
            raise

    async def _close_impl(self) -> None:
        errors: list[BaseException] = []
        try:
            try:
                await self.confirmation_client_disconnected()
            except BaseException as exc:
                errors.append(exc)

            pending_ids = (
                set(self._terminal_waiters)
                | set(self._pending_arguments)
            )
            for request_id in pending_ids:
                try:
                    current = await self.repository.cancel_request(
                        request_id,
                        self.clock(),
                    )
                    self._pending_arguments.pop(request_id, None)
                    self._pending_model_confirmations.discard(request_id)
                    if current is not None:
                        confirmation = (
                            await self.repository.get_confirmation_for_request(
                                request_id
                            )
                        )
                        self._resolve_terminal_waiters(
                            self._to_view(current, confirmation)
                        )
                except BaseException as exc:
                    errors.append(exc)

            running = tuple(self._running_tasks.items())
            for request_id, task in running:
                if task.done():
                    continue
                try:
                    await self.repository.cancel_request(
                        request_id,
                        self.clock(),
                    )
                    self._cancel_requested.add(request_id)
                    task.cancel()
                except BaseException as exc:
                    errors.append(exc)
            if running:
                await asyncio.gather(
                    *(task for _, task in running),
                    return_exceptions=True,
                )
        finally:
            if not self._unsubscribed_terminal_events:
                try:
                    self._unsubscribe_terminal_events()
                except BaseException as exc:
                    errors.append(exc)
                else:
                    self._unsubscribed_terminal_events = True

        if errors:
            raise errors[0]
        self._closed = True

    async def confirmation_client_disconnected(self) -> None:
        updates: list[_DeferredToolUpdate] = []
        async with self._confirmation_lock:
            self._confirmation_generation += 1
            for request_id in tuple(self._pending_model_confirmations):
                updates.append(
                    await self._fail_pending_model_confirmation_locked(
                        request_id
                    )
                )
        for update in updates:
            await self._publish_deferred_update(update)

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
        async with self._execution_lock:
            current = await self.repository.get_request(record.request_id)
            if current is None:
                raise ToolStateConflictError("tool request was not found")
            if current.state is not ToolRequestState.RUNNING:
                confirmation = (
                    await self.repository.get_confirmation_for_request(
                        record.request_id
                    )
                )
                view = self._to_view(current, confirmation)
                self._resolve_terminal_waiters(view)
                return view
            publish_after = asyncio.Event()
            publish_after.set()
            supervisor = asyncio.create_task(
                self._supervise_execution(
                    record,
                    definition,
                    validated_arguments,
                    publish_after=publish_after,
                    requires_live_confirmation=False,
                )
            )
            self._register_running_task(record.request_id, supervisor)
        return await asyncio.shield(supervisor)

    async def _supervise_execution(
        self,
        record: ToolRequestRecord,
        definition: ToolDefinition,
        validated_arguments: BaseModel,
        *,
        publish_after: asyncio.Event,
        requires_live_confirmation: bool,
    ) -> ToolRequestView:
        current_task = asyncio.current_task()
        cancelled_by_service = False
        try:
            execution = (
                self._run_confirmed_model_handler(
                    record,
                    definition,
                    validated_arguments,
                )
                if requires_live_confirmation
                else definition.handler(validated_arguments)
            )
            result = await asyncio.wait_for(
                execution,
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
            cancelled_by_service = (
                record.request_id in self._cancel_requested
            )
            if not cancelled_by_service:
                if self._running_tasks.get(record.request_id) is current_task:
                    self._running_tasks.pop(record.request_id, None)
                self._cancel_requested.discard(record.request_id)
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
        try:
            if final is None:
                raise ToolStateConflictError("tool request state was lost")
            confirmation = await self.repository.get_confirmation_for_request(
                record.request_id
            )
            view = self._to_view(final, confirmation)
            self._resolve_terminal_waiters(view)
            if cancelled_by_service:
                return view
            await publish_after.wait()
            await self.publisher.publish(
                ToolEvent(
                    type=ToolEventType.REQUEST_UPDATED,
                    request=view,
                )
            )
            return view
        finally:
            if self._running_tasks.get(record.request_id) is current_task:
                self._running_tasks.pop(record.request_id, None)
            self._cancel_requested.discard(record.request_id)

    def _register_running_task(
        self,
        request_id: str,
        task: asyncio.Task,
    ) -> None:
        self._running_tasks[request_id] = task
        task.add_done_callback(
            lambda completed: self._finalize_running_task(
                request_id,
                completed,
            )
        )

    def _finalize_running_task(
        self,
        request_id: str,
        task: asyncio.Task,
    ) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        finally:
            if self._running_tasks.get(request_id) is task:
                self._running_tasks.pop(request_id, None)
            self._cancel_requested.discard(request_id)

    def _confirmation_is_online(self) -> bool:
        try:
            return self.confirmation_client_online() is True
        except Exception:
            return False

    @staticmethod
    def _summarize_confirmation(
        definition: ToolDefinition,
        validated_arguments: BaseModel,
    ) -> dict[str, Any]:
        if definition.confirmation_summary is None:
            return summarize_arguments(
                validated_arguments.model_dump(mode="json"),
                definition.sensitive_fields,
            )
        try:
            summary = definition.confirmation_summary(validated_arguments)
        except Exception as exc:
            raise ToolArgumentsError(
                "tool confirmation summary is invalid"
            ) from exc
        if not isinstance(summary, dict):
            raise ToolArgumentsError("tool confirmation summary is invalid")
        try:
            return json.loads(
                json.dumps(summary, allow_nan=False, separators=(",", ":"))
            )
        except (TypeError, ValueError) as exc:
            raise ToolArgumentsError(
                "tool confirmation summary is invalid"
            ) from exc

    async def _expire_waiting_request(
        self,
        request_id: str,
    ) -> ToolRequestView:
        confirmation = await self.repository.get_confirmation_for_request(
            request_id
        )
        if confirmation is None:
            current = await self.get_request(request_id)
            if current is None:
                raise ToolStateConflictError("tool request was not found")
            return current
        claim = await self.repository.claim_decision(
            confirmation.confirmation_id,
            ToolDecision.REJECT,
            max(self.clock(), confirmation.expires_at),
        )
        if claim is None:
            raise ToolStateConflictError("confirmation was not found")
        view = self._to_view(claim.request, claim.confirmation)
        if self._is_terminal(view.state):
            self._pending_arguments.pop(request_id, None)
            self._pending_model_confirmations.discard(request_id)
            await self.publisher.publish(
                ToolEvent(
                    type=ToolEventType.CONFIRMATION_UPDATED,
                    request=view,
                )
            )
            self._resolve_terminal_waiters(view)
        return view

    async def _fail_running_wait(
        self,
        request_id: str,
    ) -> ToolRequestView:
        async with self._execution_lock:
            failed = await self.repository.transition_request(
                request_id,
                {ToolRequestState.RUNNING},
                ToolRequestState.FAILED,
                error_code="execution_timeout",
                event=self._audit(
                    request_id,
                    "failed",
                    self.clock(),
                ),
            )
            final = failed or await self.repository.get_request(request_id)
            if final is not None and final.state is ToolRequestState.RUNNING:
                final = await self.repository.cancel_request(
                    request_id,
                    self.clock(),
                )
            task = self._running_tasks.get(request_id)
            if task is not None and not task.done():
                self._cancel_requested.add(request_id)
                task.cancel()
        if final is None or not self._is_terminal(final.state):
            raise ToolStateConflictError("tool request did not reach terminal state")
        confirmation = await self.repository.get_confirmation_for_request(
            request_id
        )
        view = self._to_view(final, confirmation)
        await self.publisher.publish(
            ToolEvent(
                type=ToolEventType.REQUEST_UPDATED,
                request=view,
            )
        )
        self._resolve_terminal_waiters(view)
        return view

    async def _fail_pending_model_confirmation(
        self,
        request_id: str,
    ) -> ToolRequestView:
        async with self._confirmation_lock:
            update = await self._fail_pending_model_confirmation_locked(
                request_id
            )
        await self._publish_deferred_update(update)
        return update.view

    async def _fail_pending_model_confirmation_locked(
        self,
        request_id: str,
    ) -> _DeferredToolUpdate:
        self._pending_model_confirmations.discard(request_id)
        self._pending_arguments.pop(request_id, None)
        cancelled = await self.repository.cancel_request(
            request_id,
            self.clock(),
        )
        if cancelled is None:
            raise ToolStateConflictError("tool request was not found")
        failed = await self.repository.transition_request(
            request_id,
            {ToolRequestState.CANCELLED},
            ToolRequestState.FAILED,
            error_code="confirmation_client_unavailable",
            event=self._audit(
                request_id,
                "confirmation_client_unavailable",
                self.clock(),
            ),
        )
        final = failed or await self.repository.get_request(request_id)
        if final is None:
            raise ToolStateConflictError("tool request was not found")
        confirmation = await self.repository.get_confirmation_for_request(
            request_id
        )
        view = self._to_view(final, confirmation)
        running_task = self._running_tasks.get(request_id)
        if (
            running_task is not None
            and not running_task.done()
            and running_task is not asyncio.current_task()
        ):
            self._cancel_requested.add(request_id)
            running_task.cancel()
        return _DeferredToolUpdate(
            view=view,
            event_type=ToolEventType.CONFIRMATION_UPDATED,
            resolve_terminal=True,
        )

    async def _run_confirmed_model_handler(
        self,
        record: ToolRequestRecord,
        definition: ToolDefinition,
        validated_arguments: BaseModel,
    ) -> dict[str, Any]:
        handler_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._confirmation_lock:
            async with self._execution_lock:
                if (
                    record.request_id not in self._pending_model_confirmations
                    or not self._confirmation_is_online()
                ):
                    if record.request_id in self._pending_model_confirmations:
                        await self._fail_pending_model_confirmation_locked(
                            record.request_id
                        )
                else:
                    current = await self.repository.get_request(
                        record.request_id
                    )
                    if (
                        current is not None
                        and current.state is ToolRequestState.RUNNING
                    ):
                        self._pending_model_confirmations.discard(
                            record.request_id
                        )
                        self._pending_arguments.pop(record.request_id, None)
                        handler_task = asyncio.create_task(
                            definition.handler(validated_arguments)
                        )
        if handler_task is None:
            raise ToolExecutionError("confirmation_client_unavailable")
        return await handler_task

    async def _publish_deferred_update(
        self,
        update: _DeferredToolUpdate,
    ) -> None:
        await self.publisher.publish(
            ToolEvent(
                type=update.event_type,
                request=update.view,
            )
        )
        if update.resolve_terminal:
            self._resolve_terminal_waiters(update.view)

    @staticmethod
    def _is_terminal(state: ToolRequestState) -> bool:
        return state in {
            ToolRequestState.SUCCEEDED,
            ToolRequestState.FAILED,
            ToolRequestState.REJECTED,
            ToolRequestState.EXPIRED,
            ToolRequestState.CANCELLED,
        }

    def _resolve_terminal_waiters(self, view: ToolRequestView) -> None:
        if not self._is_terminal(view.state):
            return
        for waiter in tuple(self._terminal_waiters.get(view.request_id, ())):
            if not waiter.done():
                waiter.set_result(view)

    async def _receive_terminal_event(self, event: ToolEvent) -> None:
        self._resolve_terminal_waiters(event.request)

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
            serialized = json.dumps(
                arguments,
                allow_nan=False,
                separators=(",", ":"),
            )
            normalized_arguments = json.loads(serialized)
            validated_arguments = (
                definition.arguments_model.model_validate_json(
                    serialized,
                    strict=True,
                )
            )
            reject_additional_arguments(
                normalized_arguments,
                validated_arguments,
            )
            return validated_arguments
        except (TypeError, ValueError, ValidationError) as exc:
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
