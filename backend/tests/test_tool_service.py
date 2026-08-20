import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from computer.models import ComputerPlatform, ModelAccess
from domain.messages import MessageSource
from domain.tools import (
    ConfirmationState,
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolDecision,
    ToolDecisionClaim,
    ToolEventType,
    ToolRequest,
    ToolRequestRecord,
    ToolRequestState,
    ToolRisk,
    ToolSource,
)
from tools.registry import ToolDefinition, ToolNotFoundError, ToolRegistry
from tools.catalog import ModelToolCallContext
from tools.service import (
    ToolArgumentsError,
    ToolExecutionService,
    ToolStateConflictError,
)


class Arguments(BaseModel):
    value: str = "ok"
    token: str = "private"
    count: int = 1


class Operation(str, Enum):
    READ = "read"


class NestedArguments(BaseModel):
    label: str


class JsonArguments(BaseModel):
    operation: Operation
    requested_at: datetime
    nested: NestedArguments
    count: int


class RuntimeArguments(BaseModel):
    nested: NestedArguments
    count: int


class NamedMapping(RootModel[dict[str, NestedArguments]]):
    pass


class MappingArguments(BaseModel):
    direct: dict[str, NestedArguments]
    arrays: list[dict[str, NestedArguments]]
    referenced: NamedMapping


class IntegerValue(BaseModel):
    value: int
    a_note: str | None = None


class StringValue(BaseModel):
    value: str


class CatValue(BaseModel):
    kind: Literal["cat"]
    value: int
    cat_note: str | None = None


class DogValue(BaseModel):
    kind: Literal["dog"]
    value: int
    dog_note: str | None = None


class UnionArguments(BaseModel):
    overlap: IntegerValue | StringValue
    pet: Annotated[
        CatValue | DogValue,
        Field(discriminator="kind"),
    ]
    items: list[IntegerValue | StringValue]


class ExtraAllowedNestedArguments(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str


class ExtraAllowedArguments(BaseModel):
    model_config = ConfigDict(extra="allow")

    nested: ExtraAllowedNestedArguments


class NestedAliasArguments(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    nested_value: str = Field(alias="nestedValue")


class AliasArguments(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    regular_value: str = Field(alias="regularValue")
    validation_value: str = Field(validation_alias="validationValue")
    choice_value: str = Field(
        validation_alias=AliasChoices("choiceValue", "legacyChoice")
    )
    nested: NestedAliasArguments


class InMemoryToolRepository:
    def __init__(self):
        self.requests: dict[str, ToolRequestRecord] = {}
        self.confirmations: dict[str, ToolConfirmationRecord] = {}
        self.events: list[ToolAuditEvent] = []
        self._lock = asyncio.Lock()

    async def create_request(
        self,
        record: ToolRequestRecord,
        events: list[ToolAuditEvent],
    ) -> None:
        async with self._lock:
            self.requests[record.request_id] = record
            self.events.extend(events)

    async def create_confirmation(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
        events: list[ToolAuditEvent],
    ) -> None:
        async with self._lock:
            self.requests[request.request_id] = request
            self.confirmations[confirmation.confirmation_id] = confirmation
            self.events.extend(events)

    async def claim_decision(
        self,
        confirmation_id: str,
        decision: ToolDecision,
        now: datetime,
    ) -> ToolDecisionClaim | None:
        async with self._lock:
            confirmation = self.confirmations.get(confirmation_id)
            if confirmation is None:
                return None
            request = self.requests[confirmation.request_id]
            if confirmation.state is not ConfirmationState.PENDING:
                return ToolDecisionClaim(
                    request=request,
                    confirmation=confirmation,
                    claimed=False,
                )

            if now >= confirmation.expires_at:
                confirmation_state = ConfirmationState.EXPIRED
                request_state = ToolRequestState.EXPIRED
                event_type = "expired"
            elif decision is ToolDecision.REJECT:
                confirmation_state = ConfirmationState.REJECTED
                request_state = ToolRequestState.REJECTED
                event_type = "rejected"
            else:
                confirmation_state = ConfirmationState.APPROVED
                request_state = ToolRequestState.RUNNING
                event_type = "approved"

            updated_confirmation = confirmation.model_copy(
                update={
                    "state": confirmation_state,
                    "decided_at": now,
                }
            )
            updated_request = request.model_copy(
                update={"state": request_state, "updated_at": now}
            )
            self.confirmations[confirmation_id] = updated_confirmation
            self.requests[request.request_id] = updated_request
            self.events.append(
                ToolAuditEvent(
                    request_id=request.request_id,
                    event_type=event_type,
                    created_at=now,
                )
            )
            return ToolDecisionClaim(
                request=updated_request,
                confirmation=updated_confirmation,
                claimed=True,
            )

    async def transition_request(
        self,
        request_id: str,
        expected: set[ToolRequestState],
        state: ToolRequestState,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        event: ToolAuditEvent,
    ) -> ToolRequestRecord | None:
        async with self._lock:
            request = self.requests.get(request_id)
            if request is None or request.state not in expected:
                return None
            updated = request.model_copy(
                update={
                    "state": state,
                    "result": result,
                    "error_code": error_code,
                    "updated_at": event.created_at,
                }
            )
            self.requests[request_id] = updated
            self.events.append(event)
            return updated

    async def cancel_request(
        self,
        request_id: str,
        now: datetime,
    ) -> ToolRequestRecord | None:
        async with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                return None
            if request.state not in {
                ToolRequestState.PENDING_CONFIRMATION,
                ToolRequestState.RUNNING,
            }:
                return request
            updated = request.model_copy(
                update={
                    "state": ToolRequestState.CANCELLED,
                    "updated_at": now,
                }
            )
            self.requests[request_id] = updated
            for confirmation_id, confirmation in tuple(
                self.confirmations.items()
            ):
                if (
                    confirmation.request_id == request_id
                    and confirmation.state is ConfirmationState.PENDING
                ):
                    self.confirmations[confirmation_id] = (
                        confirmation.model_copy(
                            update={
                                "state": ConfirmationState.CANCELLED,
                                "decided_at": now,
                            }
                        )
                    )
            self.events.append(
                ToolAuditEvent(
                    request_id=request_id,
                    event_type="cancelled",
                    created_at=now,
                )
            )
            return updated

    async def get_request(
        self,
        request_id: str,
    ) -> ToolRequestRecord | None:
        return self.requests.get(request_id)

    async def get_confirmation(
        self,
        confirmation_id: str,
    ) -> ToolConfirmationRecord | None:
        return self.confirmations.get(confirmation_id)

    async def get_confirmation_for_request(
        self,
        request_id: str,
    ) -> ToolConfirmationRecord | None:
        return next(
            (
                confirmation
                for confirmation in self.confirmations.values()
                if confirmation.request_id == request_id
            ),
            None,
        )

    async def list_pending_confirmations(
        self,
        now: datetime,
    ) -> list[ToolConfirmationRecord]:
        pending = []
        for confirmation in tuple(self.confirmations.values()):
            if confirmation.state is not ConfirmationState.PENDING:
                continue
            if now >= confirmation.expires_at:
                await self.claim_decision(
                    confirmation.confirmation_id,
                    ToolDecision.REJECT,
                    now,
                )
                continue
            pending.append(confirmation)
        return pending


def build_service(
    *,
    risk: ToolRisk,
    handler,
    timeout_seconds: float = 1,
    cancellable: bool = True,
    clock=None,
    allowed_sources: frozenset[ToolSource] | None = None,
    arguments_model: type[BaseModel] = Arguments,
    model_access: ModelAccess = ModelAccess.HIDDEN,
    confirmation_client_online=lambda: False,
    platform: ComputerPlatform | None = ComputerPlatform.MACOS,
    runtime_profile: str | None = "desktop",
    allowed_channels: frozenset[MessageSource] | None = None,
    allowed_model_tool_names: frozenset[str] | None = None,
):
    repository = InMemoryToolRepository()
    registry = ToolRegistry()
    definition_values = dict(
        name="example.tool",
        title="示例工具",
        arguments_model=arguments_model,
        risk=risk,
        impact="修改示例目标" if risk is ToolRisk.HIGH else "只读取示例",
        timeout_seconds=timeout_seconds,
        cancellable=cancellable,
        sensitive_fields=frozenset({"token"}),
        handler=handler,
        model_access=model_access,
    )
    if allowed_sources is not None:
        definition_values["allowed_sources"] = allowed_sources
    if allowed_channels is not None:
        definition_values["allowed_channels"] = allowed_channels
    registry.register(ToolDefinition(**definition_values))
    service = ToolExecutionService(
        registry=registry,
        repository=repository,
        confirmation_timeout_seconds=60,
        clock=clock,
        confirmation_client_online=confirmation_client_online,
        platform=platform,
        runtime_profile=runtime_profile,
        allowed_model_tool_names=allowed_model_tool_names,
    )
    return service, repository


def request() -> ToolRequest:
    return ToolRequest(
        correlation_id="message-1",
        source=ToolSource.DESKTOP,
        origin=MessageSource.DESKTOP,
        tool_name="example.tool",
        arguments={"value": "hello", "token": "private-token"},
    )


def model_context(
    channel: MessageSource = MessageSource.DESKTOP,
) -> ModelToolCallContext:
    return ModelToolCallContext(
        channel=channel,
        advertised_tool_names=frozenset({"example.tool"}),
    )


class ToolExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_model_proposal_waits_for_online_confirmation(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"done": True}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        proposed = request().model_copy(
            update={"source": ToolSource.MODEL}
        )

        pending = await service.request(
            proposed,
            model_context=model_context(),
        )
        waiter = asyncio.create_task(
            service.wait_for_terminal(pending.request_id, timeout=1)
        )
        await asyncio.sleep(0)
        approved = await service.decide(
            pending.confirmation.id,
            ToolDecision.APPROVE,
        )
        terminal = await waiter

        self.assertEqual(approved.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(terminal.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(calls, 1)
        self.assertIs(
            repository.requests[pending.request_id].origin,
            MessageSource.DESKTOP,
        )

    async def test_model_proposal_fails_closed_for_untrusted_context(self):
        async def handler(_: Arguments) -> dict:
            raise AssertionError("high-risk action must not execute")

        cases = (
            (MessageSource.QQ, True, ComputerPlatform.MACOS, "desktop", None),
            (
                MessageSource.DESKTOP,
                False,
                ComputerPlatform.MACOS,
                "desktop",
                None,
            ),
            (MessageSource.DESKTOP, True, None, "desktop", None),
            (
                MessageSource.DESKTOP,
                True,
                ComputerPlatform.MACOS,
                "cloud",
                None,
            ),
            (
                MessageSource.DESKTOP,
                True,
                ComputerPlatform.MACOS,
                "desktop",
                frozenset({MessageSource.QQ}),
            ),
        )
        for origin, online, platform, profile, channels in cases:
            with self.subTest(
                origin=origin,
                online=online,
                platform=platform,
                profile=profile,
                channels=channels,
            ):
                service, repository = build_service(
                    risk=ToolRisk.HIGH,
                    handler=handler,
                    allowed_sources=frozenset({ToolSource.MODEL}),
                    model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
                    confirmation_client_online=lambda value=online: value,
                    platform=platform,
                    runtime_profile=profile,
                    allowed_channels=channels,
                )
                proposed = request().model_copy(
                    update={
                        "source": ToolSource.MODEL,
                        "origin": origin,
                    }
                )

                with self.assertRaises(ToolNotFoundError):
                    await service.request(
                        proposed,
                        model_context=model_context(origin),
                    )

                self.assertEqual(repository.requests, {})

    async def test_closing_service_releases_confirmation_waiter_safely(self):
        async def handler(_: Arguments) -> dict:
            raise AssertionError("closed confirmation must not execute")

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        waiter = asyncio.create_task(
            service.wait_for_terminal(pending.request_id, timeout=1)
        )
        await asyncio.sleep(0)

        await service.aclose()
        terminal = await waiter

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(
            terminal.error_code,
            "confirmation_client_unavailable",
        )

    async def test_last_confirmation_client_disconnect_persists_failure(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        waiter = asyncio.create_task(
            service.wait_for_terminal(pending.request_id, timeout=1)
        )
        await asyncio.sleep(0)

        await service.confirmation_client_disconnected()
        terminal = await waiter
        persisted = await service.get_request(pending.request_id)
        late_approval = await service.decide(
            pending.confirmation.id,
            ToolDecision.APPROVE,
        )

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(persisted.state, ToolRequestState.FAILED)
        self.assertEqual(late_approval.state, ToolRequestState.FAILED)
        self.assertEqual(
            persisted.error_code,
            "confirmation_client_unavailable",
        )
        self.assertEqual(calls, 0)
        self.assertNotIn(pending.request_id, service._pending_arguments)
        self.assertEqual(
            repository.confirmations[pending.confirmation.id].state,
            ConfirmationState.CANCELLED,
        )

    async def test_approval_rechecks_live_confirmation_client_status(self):
        online = True
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: online,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        online = False

        terminal = await service.decide(
            pending.confirmation.id,
            ToolDecision.APPROVE,
        )

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(
            terminal.error_code,
            "confirmation_client_unavailable",
        )
        self.assertEqual(calls, 0)

    async def test_disconnect_serializes_a_racing_approval_before_execution(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        disconnect_entered = asyncio.Event()
        release_disconnect = asyncio.Event()
        fail_pending = service._fail_pending_model_confirmation_locked
        approval_claim_entered = asyncio.Event()
        claim_decision = service.repository.claim_decision

        async def blocked_fail_pending(request_id: str):
            disconnect_entered.set()
            await release_disconnect.wait()
            return await fail_pending(request_id)

        async def observed_claim_decision(*args, **kwargs):
            approval_claim_entered.set()
            return await claim_decision(*args, **kwargs)

        service._fail_pending_model_confirmation_locked = blocked_fail_pending
        service.repository.claim_decision = observed_claim_decision
        disconnect = asyncio.create_task(
            service.confirmation_client_disconnected()
        )
        await disconnect_entered.wait()
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                approval_claim_entered.wait(),
                timeout=0.01,
            )

        release_disconnect.set()
        await disconnect
        terminal = await approval

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(calls, 0)

    async def test_disconnect_after_execution_claim_prevents_handler_registration(self):
        online = True
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"done": True}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: online,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        execution_claimed = asyncio.Event()
        release_claim = asyncio.Event()
        transition_request = repository.transition_request

        async def block_after_execution_claim(
            request_id,
            expected,
            state,
            **kwargs,
        ):
            result = await transition_request(
                request_id,
                expected,
                state,
                **kwargs,
            )
            if (
                expected == {ToolRequestState.RUNNING}
                and state is ToolRequestState.RUNNING
            ):
                execution_claimed.set()
                await release_claim.wait()
            return result

        repository.transition_request = block_after_execution_claim
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        await execution_claimed.wait()
        online = False
        disconnect = asyncio.create_task(
            service.confirmation_client_disconnected()
        )
        await asyncio.sleep(0)
        release_claim.set()

        terminal, _ = await asyncio.gather(approval, disconnect)

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(
            terminal.error_code,
            "confirmation_client_unavailable",
        )
        self.assertEqual(calls, 0)

    async def test_confirmation_update_subscriber_can_disconnect_without_deadlock(self):
        async def handler(_: Arguments) -> dict:
            raise AssertionError("rejected action must not execute")

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )

        async def disconnect_on_update(event):
            if event.type is ToolEventType.CONFIRMATION_UPDATED:
                await service.confirmation_client_disconnected()

        service.publisher.subscribe(disconnect_on_update)
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )

        terminal = await asyncio.wait_for(
            service.decide(
                pending.confirmation.id,
                ToolDecision.REJECT,
            ),
            timeout=0.1,
        )

        self.assertEqual(terminal.state, ToolRequestState.REJECTED)

    async def test_execution_timeout_runs_during_confirmation_update_publish(self):
        calls = 0
        handler_cancelled = asyncio.Event()
        confirmation_update_entered = asyncio.Event()
        release_confirmation_update = asyncio.Event()

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                handler_cancelled.set()
                raise

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            timeout_seconds=0.01,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )

        async def block_confirmation_update(event):
            if event.type is ToolEventType.CONFIRMATION_UPDATED:
                confirmation_update_entered.set()
                await release_confirmation_update.wait()

        service.publisher.subscribe(block_confirmation_update)
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        await confirmation_update_entered.wait()

        try:
            await asyncio.wait_for(handler_cancelled.wait(), timeout=0.1)
        finally:
            release_confirmation_update.set()
        terminal = await approval

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(terminal.error_code, "execution_timeout")
        self.assertEqual(calls, 1)

    async def test_cancelled_approval_does_not_orphan_execution_supervisor(self):
        calls = 0
        handler_entered = asyncio.Event()
        release_handler = asyncio.Event()
        confirmation_update_entered = asyncio.Event()
        release_confirmation_update = asyncio.Event()

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            handler_entered.set()
            await release_handler.wait()
            return {"done": True}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )

        async def block_confirmation_update(event):
            if event.type is ToolEventType.CONFIRMATION_UPDATED:
                confirmation_update_entered.set()
                await release_confirmation_update.wait()

        service.publisher.subscribe(block_confirmation_update)
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        await confirmation_update_entered.wait()
        await handler_entered.wait()
        supervisor = service._running_tasks[pending.request_id]

        approval.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await approval
        release_confirmation_update.set()
        release_handler.set()

        terminal = await service.wait_for_terminal(
            pending.request_id,
            timeout=1,
        )
        await asyncio.wait_for(
            asyncio.shield(supervisor),
            timeout=1,
        )

        self.assertEqual(terminal.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(calls, 1)
        self.assertTrue(supervisor.done())
        self.assertNotIn(pending.request_id, service._running_tasks)

    async def test_failed_execution_claim_transition_never_runs_handler(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        transition_request = repository.transition_request

        async def reject_execution_claim(
            request_id,
            expected,
            state,
            **kwargs,
        ):
            if (
                expected == {ToolRequestState.RUNNING}
                and state is ToolRequestState.RUNNING
            ):
                current = repository.requests[request_id]
                repository.requests[request_id] = current.model_copy(
                    update={"state": ToolRequestState.CANCELLED}
                )
                return None
            return await transition_request(
                request_id,
                expected,
                state,
                **kwargs,
            )

        repository.transition_request = reject_execution_claim

        terminal = await service.decide(
            pending.confirmation.id,
            ToolDecision.APPROVE,
        )

        self.assertEqual(terminal.state, ToolRequestState.CANCELLED)
        self.assertEqual(calls, 0)

    async def test_terminal_wait_timeout_expires_without_execution(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )

        terminal = await service.wait_for_terminal(
            pending.request_id,
            timeout=0.001,
        )

        self.assertEqual(terminal.state, ToolRequestState.EXPIRED)
        self.assertEqual(calls, 0)

    async def test_terminal_wait_survives_approval_timeout_race(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"done": True}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        execution_claim_entered = asyncio.Event()
        release_execution_claim = asyncio.Event()
        transition_request = repository.transition_request

        async def blocked_execution_claim(
            request_id,
            expected,
            state,
            **kwargs,
        ):
            if (
                expected == {ToolRequestState.RUNNING}
                and state is ToolRequestState.RUNNING
            ):
                execution_claim_entered.set()
                await release_execution_claim.wait()
            return await transition_request(
                request_id,
                expected,
                state,
                **kwargs,
            )

        repository.transition_request = blocked_execution_claim
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        await execution_claim_entered.wait()
        waiter = asyncio.create_task(
            service.wait_for_terminal(pending.request_id, timeout=0.001)
        )
        await asyncio.sleep(0.01)
        release_execution_claim.set()

        approved, terminal = await asyncio.gather(approval, waiter)

        self.assertEqual(approved.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(terminal.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(calls, 1)

    async def test_execution_timeout_prevents_late_handler_start(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"done": True}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            timeout_seconds=0.02,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.PROPOSE_WITH_CONFIRMATION,
            confirmation_client_online=lambda: True,
        )
        pending = await service.request(
            request().model_copy(update={"source": ToolSource.MODEL}),
            model_context=model_context(),
        )
        handler_entry_entered = asyncio.Event()
        release_handler_entry = asyncio.Event()
        run_confirmed_handler = service._run_confirmed_model_handler

        async def delayed_handler_entry(*args, **kwargs):
            handler_entry_entered.set()
            await release_handler_entry.wait()
            return await run_confirmed_handler(*args, **kwargs)

        service._run_confirmed_model_handler = delayed_handler_entry
        approval = asyncio.create_task(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            )
        )
        await handler_entry_entered.wait()

        terminal = await service.wait_for_terminal(
            pending.request_id,
            timeout=0.001,
        )
        release_handler_entry.set()
        approved = await approval

        self.assertEqual(terminal.state, ToolRequestState.FAILED)
        self.assertEqual(terminal.error_code, "execution_timeout")
        self.assertEqual(approved.state, ToolRequestState.FAILED)
        self.assertEqual(calls, 0)

    async def test_aclose_is_retryable_after_partial_failure(self):
        async def handler(_: Arguments) -> dict:
            return {}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
        )
        attempts = 0

        async def flaky_disconnect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary close failure")

        service.confirmation_client_disconnected = flaky_disconnect

        with self.assertRaisesRegex(RuntimeError, "temporary close failure"):
            await service.aclose()

        self.assertFalse(service._closed)
        self.assertNotIn(
            service._receive_terminal_event,
            service.publisher._subscribers,
        )

        await service.aclose()

        self.assertTrue(service._closed)
        self.assertEqual(attempts, 2)

    async def test_concurrent_aclose_callers_share_one_closing_task(self):
        async def handler(_: Arguments) -> dict:
            return {}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = 0

        async def blocked_disconnect():
            nonlocal attempts
            attempts += 1
            entered.set()
            await release.wait()

        service.confirmation_client_disconnected = blocked_disconnect
        first = asyncio.create_task(service.aclose())
        await entered.wait()
        second = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)

        self.assertFalse(second.done())

        release.set()

        await asyncio.gather(first, second)

        self.assertEqual(attempts, 1)
        self.assertTrue(service._closed)

    async def test_string_source_cannot_bypass_source_policy(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        for source, risk, allowed_sources in (
            ("desktop", ToolRisk.LOW, None),
            ("model", ToolRisk.HIGH, frozenset({ToolSource.MODEL})),
            ("invalid", ToolRisk.LOW, None),
        ):
            with self.subTest(source=source):
                service, repository = build_service(
                    risk=risk,
                    handler=handler,
                    allowed_sources=allowed_sources,
                )
                invalid_request = request().model_copy(
                    update={"source": source}
                )

                with self.assertRaises(ToolNotFoundError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_model_cannot_execute_low_risk_tool_without_authorization(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
        )
        model_request = request().model_copy(
            update={
                "source": ToolSource.MODEL,
                "arguments": {"value": 123},
            }
        )

        with self.assertRaises(ToolNotFoundError):
            await service.request(model_request)

        self.assertEqual(calls, 0)
        self.assertEqual(repository.requests, {})
        self.assertEqual(repository.confirmations, {})

    async def test_model_low_risk_execution_uses_only_trusted_context(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"read": True}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.READ_ONLY,
            allowed_channels=frozenset({MessageSource.DESKTOP}),
        )
        forged = request().model_copy(
            update={
                "source": ToolSource.MODEL,
                "origin": MessageSource.DESKTOP,
            }
        )

        with self.assertRaises(ToolNotFoundError):
            await service.request(forged)

        result = await service.request(
            forged.model_copy(update={"origin": MessageSource.QQ}),
            model_context=model_context(MessageSource.DESKTOP),
        )

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(calls, 1)
        self.assertIs(
            repository.requests[result.request_id].origin,
            MessageSource.DESKTOP,
        )

    async def test_cloud_model_execution_requires_exact_allowlist_and_qq(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"read": True}

        request_from_model = request().model_copy(
            update={"source": ToolSource.MODEL}
        )
        for allowlist, channel in (
            (None, MessageSource.QQ),
            (frozenset({"another.tool"}), MessageSource.QQ),
            (frozenset({"example.tool"}), MessageSource.DESKTOP),
        ):
            with self.subTest(allowlist=allowlist, channel=channel):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    allowed_sources=frozenset({ToolSource.MODEL}),
                    model_access=ModelAccess.READ_ONLY,
                    allowed_channels=frozenset(
                        {MessageSource.DESKTOP, MessageSource.QQ}
                    ),
                    runtime_profile="cloud",
                    allowed_model_tool_names=allowlist,
                )

                with self.assertRaises(ToolNotFoundError):
                    await service.request(
                        request_from_model,
                        model_context=model_context(channel),
                    )

                self.assertEqual(repository.requests, {})

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
            model_access=ModelAccess.READ_ONLY,
            allowed_channels=frozenset({MessageSource.QQ}),
            runtime_profile="cloud",
            allowed_model_tool_names=frozenset({"example.tool"}),
        )
        result = await service.request(
            request_from_model,
            model_context=model_context(MessageSource.QQ),
        )

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(calls, 1)

    async def test_model_cannot_execute_high_risk_tool_when_authorized(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            allowed_sources=frozenset({ToolSource.MODEL}),
        )
        model_request = request().model_copy(
            update={"source": ToolSource.MODEL}
        )

        with self.assertRaises(ToolNotFoundError):
            await service.request(model_request)

        self.assertEqual(calls, 0)
        self.assertEqual(repository.requests, {})
        self.assertEqual(repository.confirmations, {})

    async def test_argument_validation_is_strict(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
        )
        invalid_request = request().model_copy(
            update={
                "arguments": {
                    "value": "hello",
                    "token": "private-token",
                    "count": "2",
                }
            }
        )

        self.assertEqual(
            Arguments.model_validate(invalid_request.arguments).count,
            2,
        )
        with self.assertRaises(ToolArgumentsError):
            await service.request(invalid_request)

        self.assertEqual(calls, 0)
        self.assertEqual(repository.requests, {})

    async def test_strict_json_accepts_enum_datetime_and_nested_object(self):
        received = None

        async def handler(arguments: JsonArguments) -> dict:
            nonlocal received
            received = arguments
            return {"accepted": True}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=JsonArguments,
        )
        json_request = request().model_copy(
            update={
                "arguments": {
                    "operation": "read",
                    "requested_at": "2026-07-29T12:00:00Z",
                    "nested": {"label": "demo"},
                    "count": 2,
                }
            }
        )

        result = await service.request(json_request)

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertIs(received.operation, Operation.READ)
        self.assertEqual(
            received.requested_at,
            datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )
        self.assertIsInstance(received.nested, NestedArguments)

    async def test_strict_json_rejects_numeric_string(self):
        calls = 0

        async def handler(_: JsonArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=JsonArguments,
        )
        invalid_request = request().model_copy(
            update={
                "arguments": {
                    "operation": "read",
                    "requested_at": "2026-07-29T12:00:00Z",
                    "nested": {"label": "demo"},
                    "count": "2",
                }
            }
        )

        with self.assertRaises(ToolArgumentsError):
            await service.request(invalid_request)

        self.assertEqual(calls, 0)
        self.assertEqual(repository.requests, {})

    async def test_non_json_arguments_are_rejected_without_side_effects(self):
        calls = 0

        async def handler(_: RuntimeArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=RuntimeArguments,
        )
        invalid_request = request().model_copy(
            update={
                "arguments": {
                    "nested": {"label": "demo"},
                    "count": object(),
                }
            }
        )

        with self.assertRaises(ToolArgumentsError):
            await service.request(invalid_request)

        self.assertEqual(calls, 0)
        self.assertEqual(repository.requests, {})
        self.assertEqual(repository.confirmations, {})

    async def test_runtime_rejects_top_level_and_nested_extra_fields(self):
        calls = 0

        async def handler(_: RuntimeArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        valid_arguments = {
            "nested": {"label": "demo"},
            "count": 2,
        }
        for extra_arguments in (
            {**valid_arguments, "unexpected": True},
            {
                **valid_arguments,
                "nested": {"label": "demo", "unexpected": True},
            },
        ):
            with self.subTest(arguments=extra_arguments):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    arguments_model=RuntimeArguments,
                )
                invalid_request = request().model_copy(
                    update={"arguments": extra_arguments}
                )

                with self.assertRaises(ToolArgumentsError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_runtime_accepts_mapping_values_in_all_schema_positions(self):
        received = None

        async def handler(arguments: MappingArguments) -> dict:
            nonlocal received
            received = arguments
            return {"accepted": True}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=MappingArguments,
        )
        mapping_request = request().model_copy(
            update={
                "arguments": {
                    "direct": {"first": {"label": "direct"}},
                    "arrays": [{"second": {"label": "array"}}],
                    "referenced": {"third": {"label": "referenced"}},
                }
            }
        )

        result = await service.request(mapping_request)

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertIsInstance(received.direct["first"], NestedArguments)
        self.assertIsInstance(
            received.arrays[0]["second"],
            NestedArguments,
        )
        self.assertIsInstance(
            received.referenced.root["third"],
            NestedArguments,
        )

    async def test_runtime_rejects_extra_fields_in_mapping_values(self):
        calls = 0

        async def handler(_: MappingArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        valid_arguments = {
            "direct": {"first": {"label": "direct"}},
            "arrays": [{"second": {"label": "array"}}],
            "referenced": {"third": {"label": "referenced"}},
        }
        invalid_arguments = (
            {
                **valid_arguments,
                "direct": {
                    "first": {"label": "direct", "unexpected": True}
                },
            },
            {
                **valid_arguments,
                "arrays": [
                    {
                        "second": {
                            "label": "array",
                            "unexpected": True,
                        }
                    }
                ],
            },
            {
                **valid_arguments,
                "referenced": {
                    "third": {
                        "label": "referenced",
                        "unexpected": True,
                    }
                },
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    arguments_model=MappingArguments,
                )
                invalid_request = request().model_copy(
                    update={"arguments": arguments}
                )

                with self.assertRaises(ToolArgumentsError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_runtime_follows_actual_strict_union_branch_for_extras(self):
        calls = 0

        async def handler(_: UnionArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        valid_arguments = {
            "overlap": {"value": "string"},
            "pet": {"kind": "cat", "value": 9},
            "items": [{"value": 1, "a_note": "accepted"}],
        }
        invalid_arguments = (
            {
                **valid_arguments,
                "overlap": {"value": "string", "a_note": "dropped by B"},
            },
            {
                **valid_arguments,
                "pet": {
                    "kind": "cat",
                    "value": 9,
                    "dog_note": "dropped by Cat",
                },
            },
            {
                **valid_arguments,
                "items": [
                    {
                        "value": "string",
                        "a_note": "dropped by B",
                    }
                ],
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    arguments_model=UnionArguments,
                )
                invalid_request = request().model_copy(
                    update={"arguments": arguments}
                )

                with self.assertRaises(ToolArgumentsError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_runtime_accepts_valid_strict_union_branches(self):
        received = None

        async def handler(arguments: UnionArguments) -> dict:
            nonlocal received
            received = arguments
            return {"accepted": True}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=UnionArguments,
        )
        union_request = request().model_copy(
            update={
                "arguments": {
                    "overlap": {"value": "string"},
                    "pet": {"kind": "cat", "value": 9},
                    "items": [{"value": 1, "a_note": "accepted"}],
                }
            }
        )

        result = await service.request(union_request)

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertIsInstance(received.overlap, StringValue)
        self.assertIsInstance(received.pet, CatValue)
        self.assertIsInstance(received.items[0], IntegerValue)

    async def test_runtime_rejects_extra_allow_model_fields_consistently(self):
        calls = 0

        async def handler(_: ExtraAllowedArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        for arguments in (
            {
                "nested": {"label": "demo"},
                "unexpected": True,
            },
            {
                "nested": {
                    "label": "demo",
                    "unexpected": True,
                },
            },
        ):
            with self.subTest(arguments=arguments):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    arguments_model=ExtraAllowedArguments,
                )
                invalid_request = request().model_copy(
                    update={"arguments": arguments}
                )

                with self.assertRaises(ToolArgumentsError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_runtime_rejects_duplicate_alias_inputs(self):
        calls = 0

        async def handler(_: AliasArguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        valid_arguments = {
            "regularValue": "regular",
            "validationValue": "validation",
            "choiceValue": "choice",
            "nested": {"nestedValue": "nested"},
        }
        invalid_arguments = (
            {
                **valid_arguments,
                "regular_value": "duplicate",
            },
            {
                **valid_arguments,
                "validation_value": "duplicate",
            },
            {
                **valid_arguments,
                "legacyChoice": "duplicate",
            },
            {
                **valid_arguments,
                "nested": {
                    "nestedValue": "nested",
                    "nested_value": "duplicate",
                },
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                service, repository = build_service(
                    risk=ToolRisk.LOW,
                    handler=handler,
                    arguments_model=AliasArguments,
                )
                invalid_request = request().model_copy(
                    update={"arguments": arguments}
                )

                with self.assertRaises(ToolArgumentsError):
                    await service.request(invalid_request)

                self.assertEqual(repository.requests, {})
                self.assertEqual(repository.confirmations, {})

        self.assertEqual(calls, 0)

    async def test_runtime_accepts_one_legal_alias_per_field(self):
        received = None

        async def handler(arguments: AliasArguments) -> dict:
            nonlocal received
            received = arguments
            return {"accepted": True}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
            arguments_model=AliasArguments,
        )
        alias_request = request().model_copy(
            update={
                "arguments": {
                    "regularValue": "regular",
                    "validationValue": "validation",
                    "legacyChoice": "choice",
                    "nested": {"nestedValue": "nested"},
                }
            }
        )

        result = await service.request(alias_request)

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(received.regular_value, "regular")
        self.assertEqual(received.validation_value, "validation")
        self.assertEqual(received.choice_value, "choice")
        self.assertEqual(received.nested.nested_value, "nested")

    async def test_low_risk_executes_automatically_and_redacts_audit(self):
        calls = 0

        async def handler(arguments: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {"value": arguments.value}

        service, repository = build_service(
            risk=ToolRisk.LOW,
            handler=handler,
        )

        result = await service.request(request())

        self.assertEqual(result.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(result.result, {"value": "hello"})
        self.assertEqual(calls, 1)
        self.assertEqual(
            repository.requests[result.request_id].arguments_summary["token"],
            "[REDACTED]",
        )
        self.assertEqual(
            repository.events[0].details["arguments"]["token"],
            "[REDACTED]",
        )
        self.assertEqual(
            [event.event_type for event in repository.events],
            ["requested", "execution_started", "succeeded"],
        )

    async def test_high_risk_waits_and_concurrent_approval_executes_once(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"done": True}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
        )
        pending = await service.request(request())

        self.assertEqual(
            pending.state,
            ToolRequestState.PENDING_CONFIRMATION,
        )
        self.assertIsNotNone(pending.confirmation)
        self.assertEqual(calls, 0)

        first, second = await asyncio.gather(
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            ),
            service.decide(
                pending.confirmation.id,
                ToolDecision.APPROVE,
            ),
        )

        self.assertEqual(calls, 1)
        self.assertIn(
            first.state,
            {ToolRequestState.RUNNING, ToolRequestState.SUCCEEDED},
        )
        self.assertIn(
            second.state,
            {ToolRequestState.RUNNING, ToolRequestState.SUCCEEDED},
        )
        final = await service.get_request(pending.request_id)
        self.assertEqual(final.state, ToolRequestState.SUCCEEDED)

    async def test_reject_and_pending_cancel_never_execute(self):
        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
        )
        rejected_pending = await service.request(request())
        rejected = await service.decide(
            rejected_pending.confirmation.id,
            ToolDecision.REJECT,
        )

        cancelled_pending = await service.request(
            request().model_copy(
                update={"request_id": "cancel-request"}
            )
        )
        cancelled = await service.cancel(cancelled_pending.request_id)

        self.assertEqual(rejected.state, ToolRequestState.REJECTED)
        self.assertEqual(cancelled.state, ToolRequestState.CANCELLED)
        self.assertEqual(calls, 0)

    async def test_expired_confirmation_cannot_execute(self):
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        current = now

        def clock() -> datetime:
            return current

        calls = 0

        async def handler(_: Arguments) -> dict:
            nonlocal calls
            calls += 1
            return {}

        service, _ = build_service(
            risk=ToolRisk.HIGH,
            handler=handler,
            clock=clock,
        )
        pending = await service.request(request())
        current = now + timedelta(seconds=61)

        expired = await service.decide(
            pending.confirmation.id,
            ToolDecision.APPROVE,
        )

        self.assertEqual(expired.state, ToolRequestState.EXPIRED)
        self.assertEqual(calls, 0)

    async def test_timeout_and_exception_return_only_stable_errors(self):
        async def slow(_: Arguments) -> dict:
            await asyncio.sleep(0.05)
            return {}

        timeout_service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=slow,
            timeout_seconds=0.001,
        )
        timed_out = await timeout_service.request(request())

        async def failing(_: Arguments) -> dict:
            raise RuntimeError("private exception body")

        failing_service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=failing,
        )
        failed = await failing_service.request(
            request().model_copy(update={"request_id": "failed-request"})
        )

        self.assertEqual(timed_out.state, ToolRequestState.FAILED)
        self.assertEqual(timed_out.error_code, "execution_timeout")
        self.assertEqual(failed.state, ToolRequestState.FAILED)
        self.assertEqual(failed.error_code, "execution_failed")
        self.assertNotIn("private exception body", failed.model_dump_json())

    async def test_running_cancel_obeys_cancellable_contract(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting(_: Arguments) -> dict:
            started.set()
            await release.wait()
            return {}

        service, _ = build_service(
            risk=ToolRisk.LOW,
            handler=waiting,
            cancellable=True,
        )
        running_request = request()
        running_task = asyncio.create_task(
            service.request(running_request)
        )
        await started.wait()

        cancelled = await service.cancel(
            request_id=running_request.request_id
        )

        self.assertEqual(cancelled.state, ToolRequestState.CANCELLED)
        result = await running_task
        self.assertEqual(result.state, ToolRequestState.CANCELLED)

        noncancellable, repository = build_service(
            risk=ToolRisk.LOW,
            handler=waiting,
            cancellable=False,
        )
        fixed_request = request().model_copy(
            update={"request_id": "noncancellable-request"}
        )
        repository.requests[fixed_request.request_id] = ToolRequestRecord(
            request_id=fixed_request.request_id,
            correlation_id=fixed_request.correlation_id,
            source=fixed_request.source,
            tool_name=fixed_request.tool_name,
            title="示例工具",
            risk=ToolRisk.LOW,
            state=ToolRequestState.RUNNING,
            arguments_summary={},
            impact="只读取示例",
            cancellable=False,
            timeout_seconds=1,
        )
        with self.assertRaises(ToolStateConflictError):
            await noncancellable.cancel(fixed_request.request_id)


if __name__ == "__main__":
    unittest.main()
