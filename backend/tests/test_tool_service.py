import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, RootModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.tools import (
    ConfirmationState,
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolDecision,
    ToolDecisionClaim,
    ToolRequest,
    ToolRequestRecord,
    ToolRequestState,
    ToolRisk,
    ToolSource,
)
from tools.registry import ToolDefinition, ToolNotFoundError, ToolRegistry
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
    )
    if allowed_sources is not None:
        definition_values["allowed_sources"] = allowed_sources
    registry.register(ToolDefinition(**definition_values))
    service = ToolExecutionService(
        registry=registry,
        repository=repository,
        confirmation_timeout_seconds=60,
        clock=clock,
    )
    return service, repository


def request() -> ToolRequest:
    return ToolRequest(
        correlation_id="message-1",
        source=ToolSource.DESKTOP,
        tool_name="example.tool",
        arguments={"value": "hello", "token": "private-token"},
    )


class ToolExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
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
