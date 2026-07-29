import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.model_tools import (
    ModelToolLimitError,
    ModelToolOrchestrationError,
    ModelToolOrchestrator,
)
from domain.tools import (
    ToolRequestState,
    ToolRequestView,
    ToolSource,
)
from llm.errors import ModelProtocolError, ModelTimeoutError
from llm.models import (
    ModelMessage,
    ModelReply,
    ModelRequest,
    ModelRole,
    ModelToolCall,
    ModelToolDefinition,
)
from tools.registry import ToolNotFoundError
from tools.service import ToolArgumentsError


def tool_definition(name: str) -> ModelToolDefinition:
    return ModelToolDefinition(
        name=name,
        description=f"调用 {name}",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


def tool_call(
    call_id: str,
    name: str = "system.current_time",
    *,
    arguments: dict | None = None,
) -> ModelToolCall:
    return ModelToolCall(
        id=call_id,
        name=name,
        arguments={} if arguments is None else arguments,
    )


def base_request() -> ModelRequest:
    return ModelRequest(
        correlation_id="message-1",
        messages=[
            ModelMessage(role=ModelRole.USER, content="现在几点？"),
        ],
    )


def succeeded_view(
    request,
    *,
    result: dict | None = None,
) -> ToolRequestView:
    return ToolRequestView(
        request_id=f"request-{request.tool_name}",
        correlation_id=request.correlation_id,
        tool=request.tool_name,
        state=ToolRequestState.SUCCEEDED,
        result={"ok": True} if result is None else result,
    )


class FakeGateway:
    def __init__(
        self,
        replies: list[ModelReply | Exception],
        *,
        model_name: str = "configured-model",
    ) -> None:
        self._replies = list(replies)
        self._model_name = model_name
        self.requests: list[ModelRequest] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("fake gateway reply queue exhausted")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeCatalog:
    def __init__(self, tools: list[ModelToolDefinition]) -> None:
        self.tools = tools
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        return tuple(self.tools)


def configured_orchestrator(
    gateway: FakeGateway,
    service,
    *,
    tools: list[ModelToolDefinition] | None = None,
    enabled: bool = True,
) -> ModelToolOrchestrator:
    return ModelToolOrchestrator(
        gateway=gateway,
        catalog=FakeCatalog(
            [tool_definition("system.current_time")]
            if tools is None
            else tools
        ),
        tool_service=service,
        enabled=enabled,
    )


class ModelToolOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_reply_finishes_without_tools(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    text="直接回答",
                    model="fake",
                    prompt_tokens=8,
                    completion_tokens=2,
                    provider_request_id="provider-1",
                )
            ]
        )
        service = AsyncMock()
        catalog = FakeCatalog([])
        orchestrator = ModelToolOrchestrator(
            gateway=gateway,
            catalog=catalog,
            tool_service=service,
            enabled=True,
        )

        result = await orchestrator.run(base_request())

        self.assertEqual(result.reply.text, "直接回答")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].model, "fake")
        self.assertEqual(result.attempts[0].status, "succeeded")
        self.assertEqual(result.attempts[0].prompt_tokens, 8)
        self.assertEqual(result.attempts[0].completion_tokens, 2)
        self.assertEqual(
            result.attempts[0].provider_request_id,
            "provider-1",
        )
        self.assertGreaterEqual(result.attempts[0].latency_ms, 0)
        self.assertEqual(catalog.list_calls, 1)
        service.request.assert_not_awaited()

    async def test_time_tool_result_is_returned_to_model(self):
        call = tool_call(
            "call-1",
            arguments={"timezone": "UTC"},
        )
        gateway = FakeGateway(
            [
                ModelReply(
                    text=None,
                    tool_calls=[call],
                    model="fake",
                    finish_reason="tool_calls",
                ),
                ModelReply(text="现在是 12:00", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.return_value = ToolRequestView(
            request_id="request-1",
            correlation_id="model-correlation",
            tool=call.name,
            state=ToolRequestState.SUCCEEDED,
            result={
                "timezone": "UTC",
                "iso": "2026-07-29T12:00:00+00:00",
            },
        )
        orchestrator = configured_orchestrator(gateway, service)

        result = await orchestrator.run(base_request())

        self.assertEqual(result.reply.text, "现在是 12:00")
        self.assertEqual(len(result.attempts), 2)
        requested = service.request.await_args.args[0]
        self.assertEqual(requested.source, ToolSource.MODEL)
        self.assertEqual(requested.tool_name, "system.current_time")
        self.assertEqual(requested.arguments, {"timezone": "UTC"})
        expected_material = "message-1\0" "0\0" "0\0" "call-1"
        self.assertEqual(
            requested.correlation_id,
            "model:"
            + hashlib.sha256(expected_material.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(len(requested.correlation_id), 70)
        self.assertEqual(gateway.requests[0].tools, gateway.requests[1].tools)
        tool_message = gateway.requests[1].messages[-1]
        self.assertEqual(tool_message.role, ModelRole.TOOL)
        self.assertEqual(tool_message.tool_call_id, "call-1")
        self.assertEqual(tool_message.name, "system.current_time")
        self.assertNotIn(": ", tool_message.content)
        self.assertEqual(
            json.loads(tool_message.content),
            {
                "call_id": "call-1",
                "name": "system.current_time",
                "state": "succeeded",
                "result": {
                    "timezone": "UTC",
                    "iso": "2026-07-29T12:00:00+00:00",
                },
                "error_code": None,
            },
        )

    async def test_tool_calls_take_priority_over_attached_reply_text(self):
        call = tool_call("call-with-text")
        gateway = FakeGateway(
            [
                ModelReply(
                    text="这不是最终答复",
                    tool_calls=[call],
                    model="fake",
                ),
                ModelReply(text="最终答复", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = lambda request: succeeded_view(request)

        result = await configured_orchestrator(
            gateway,
            service,
        ).run(base_request())

        self.assertEqual(result.reply.text, "最终答复")
        assistant_message = gateway.requests[1].messages[-2]
        self.assertEqual(assistant_message.role, ModelRole.ASSISTANT)
        self.assertIsNone(assistant_message.content)
        self.assertEqual(assistant_message.tool_calls, [call])

    async def test_multiple_tools_execute_in_model_order(self):
        events: list[str] = []
        calls = [
            tool_call("call-first", "example.first"),
            tool_call("call-second", "example.second"),
        ]

        async def request_tool(request):
            events.append(request.tool_name)
            return succeeded_view(request)

        gateway = FakeGateway(
            [
                ModelReply(tool_calls=calls, model="fake"),
                ModelReply(text="完成", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = request_tool
        orchestrator = configured_orchestrator(
            gateway,
            service,
            tools=[
                tool_definition("example.first"),
                tool_definition("example.second"),
            ],
        )

        await orchestrator.run(base_request())

        self.assertEqual(events, ["example.first", "example.second"])

    async def test_duplicate_call_id_across_rounds_is_protocol_error(self):
        repeated = tool_call("duplicate-id")
        gateway = FakeGateway(
            [
                ModelReply(tool_calls=[repeated], model="fake"),
                ModelReply(tool_calls=[repeated], model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = lambda request: succeeded_view(request)

        with self.assertRaisesRegex(
            ModelProtocolError,
            "duplicate tool call id",
        ):
            await configured_orchestrator(
                gateway,
                service,
            ).run(base_request())

        self.assertEqual(service.request.await_count, 1)

    async def test_duplicate_call_id_in_same_reply_is_protocol_error(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[
                        tool_call("duplicate-id"),
                        tool_call("duplicate-id"),
                    ],
                    model="fake",
                )
            ]
        )
        service = AsyncMock()

        with self.assertRaisesRegex(
            ModelProtocolError,
            "duplicate tool call id",
        ):
            await configured_orchestrator(
                gateway,
                service,
            ).run(base_request())

        service.request.assert_not_awaited()

    async def test_model_round_limit_stops_before_last_reply_tools_execute(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call(f"call-{index}")],
                    model="fake",
                )
                for index in range(3)
            ]
        )
        service = AsyncMock()
        service.request.side_effect = lambda request: succeeded_view(request)

        with self.assertRaises(ModelToolLimitError) as raised:
            await configured_orchestrator(
                gateway,
                service,
            ).run(base_request())

        self.assertEqual(raised.exception.code, "model_tool_round_limit")
        self.assertEqual(len(raised.exception.attempts), 3)
        self.assertIsNone(raised.exception.public_error)
        self.assertEqual(len(gateway.requests), 3)
        self.assertEqual(service.request.await_count, 2)
        self.assertNotIn("call-2", str(raised.exception))

    async def test_tool_call_limit_stops_without_executing_excess_batch(self):
        first_batch = [
            tool_call(f"first-{index}")
            for index in range(3)
        ]
        second_batch = [
            tool_call(f"second-{index}")
            for index in range(2)
        ]
        gateway = FakeGateway(
            [
                ModelReply(tool_calls=first_batch, model="fake"),
                ModelReply(tool_calls=second_batch, model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = lambda request: succeeded_view(request)

        with self.assertRaises(ModelToolLimitError) as raised:
            await configured_orchestrator(
                gateway,
                service,
            ).run(base_request())

        self.assertEqual(raised.exception.code, "model_tool_call_limit")
        self.assertEqual(len(raised.exception.attempts), 2)
        self.assertEqual(service.request.await_count, 3)

    async def test_unadvertised_tool_is_stable_error_without_service_call(self):
        guessed_name = "private.hidden"
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("hidden-1", guessed_name)],
                    model="fake",
                ),
                ModelReply(text="不可用", model="fake"),
            ]
        )
        service = AsyncMock()
        orchestrator = configured_orchestrator(
            gateway,
            service,
            tools=[tool_definition("system.current_time")],
        )

        result = await orchestrator.run(base_request())

        self.assertEqual(result.reply.text, "不可用")
        service.request.assert_not_awaited()
        payload = json.loads(gateway.requests[1].messages[-1].content)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "tool_not_available")
        self.assertIsNone(payload["result"])
        self.assertNotIn(
            "system.current_time",
            gateway.requests[1].messages[-1].content,
        )

    async def test_disabled_catalog_is_not_read_and_guessed_tool_is_not_run(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("disabled-call")],
                    model="fake",
                ),
                ModelReply(text="不可用", model="fake"),
            ]
        )
        service = AsyncMock()
        catalog = FakeCatalog([tool_definition("system.current_time")])
        orchestrator = ModelToolOrchestrator(
            gateway=gateway,
            catalog=catalog,
            tool_service=service,
            enabled=False,
        )

        await orchestrator.run(base_request())

        self.assertEqual(catalog.list_calls, 0)
        self.assertEqual(gateway.requests[0].tools, [])
        self.assertEqual(gateway.requests[1].tools, [])
        service.request.assert_not_awaited()

    async def test_missing_tool_service_returns_tool_not_available(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("missing-service")],
                    model="fake",
                ),
                ModelReply(text="不可用", model="fake"),
            ]
        )

        await configured_orchestrator(
            gateway,
            None,
        ).run(base_request())

        payload = json.loads(gateway.requests[1].messages[-1].content)
        self.assertEqual(payload["error_code"], "tool_not_available")

    async def test_tool_not_found_is_mapped_to_stable_result(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("not-found")],
                    model="fake",
                ),
                ModelReply(text="不可用", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = ToolNotFoundError(
            "system.current_time"
        )

        await configured_orchestrator(
            gateway,
            service,
        ).run(base_request())

        payload = json.loads(gateway.requests[1].messages[-1].content)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "tool_not_available")
        self.assertNotIn(
            "registered tool was not found",
            gateway.requests[1].messages[-1].content,
        )

    async def test_invalid_arguments_are_mapped_to_stable_result(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("bad-arguments")],
                    model="fake",
                ),
                ModelReply(text="参数错误", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.side_effect = ToolArgumentsError(
            "private validation detail"
        )

        await configured_orchestrator(
            gateway,
            service,
        ).run(base_request())

        payload = json.loads(gateway.requests[1].messages[-1].content)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "tool_arguments_invalid")
        self.assertNotIn(
            "private validation detail",
            gateway.requests[1].messages[-1].content,
        )

    async def test_failed_tool_view_preserves_only_stable_fields(self):
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("failed-tool")],
                    model="fake",
                ),
                ModelReply(text="执行失败", model="fake"),
            ]
        )
        service = AsyncMock()
        service.request.return_value = ToolRequestView(
            request_id="internal-request-id",
            correlation_id="internal-correlation",
            tool="system.current_time",
            state=ToolRequestState.FAILED,
            error_code="execution_timeout",
        )

        await configured_orchestrator(
            gateway,
            service,
        ).run(base_request())

        content = gateway.requests[1].messages[-1].content
        payload = json.loads(content)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["error_code"], "execution_timeout")
        self.assertNotIn("internal-request-id", content)
        self.assertNotIn("internal-correlation", content)

    async def test_gateway_failure_records_failed_attempt_and_wraps_safely(self):
        private_error = ModelTimeoutError("private provider response")
        gateway = FakeGateway(
            [
                ModelReply(
                    tool_calls=[tool_call("before-timeout")],
                    model="provider-model",
                    prompt_tokens=7,
                ),
                private_error,
            ],
            model_name="configured-model",
        )
        service = AsyncMock()
        service.request.side_effect = lambda request: succeeded_view(request)

        with self.assertRaises(ModelToolOrchestrationError) as raised:
            await configured_orchestrator(
                gateway,
                service,
            ).run(base_request())

        error = raised.exception
        self.assertEqual(error.code, "timeout_error")
        self.assertIs(error.public_error, private_error)
        self.assertEqual(len(error.attempts), 2)
        self.assertEqual(error.attempts[0].model, "provider-model")
        self.assertEqual(error.attempts[0].status, "succeeded")
        self.assertEqual(error.attempts[1].model, "configured-model")
        self.assertEqual(error.attempts[1].status, "timeout_error")
        self.assertNotIn("private provider response", str(error))

    async def test_first_gateway_failure_is_recorded(self):
        gateway = FakeGateway(
            [ModelTimeoutError("private provider response")],
            model_name="configured-model",
        )

        with self.assertRaises(ModelToolOrchestrationError) as raised:
            await configured_orchestrator(
                gateway,
                AsyncMock(),
            ).run(base_request())

        self.assertEqual(len(raised.exception.attempts), 1)
        self.assertEqual(
            raised.exception.attempts[0].status,
            "timeout_error",
        )


if __name__ == "__main__":
    unittest.main()
