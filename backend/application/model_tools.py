import json
from hashlib import sha256
from time import perf_counter

from domain.messages import MessageSource
from domain.tools import (
    ToolRequest,
    ToolRequestState,
    ToolRequestView,
    ToolSource,
)
from llm.errors import (
    ModelGatewayError,
    ModelProtocolError,
)
from llm.gateway import LanguageModelGateway
from llm.models import (
    ModelAttempt,
    ModelMessage,
    ModelOrchestrationResult,
    ModelReply,
    ModelRequest,
    ModelRole,
    ModelToolCall,
    ModelToolResult,
)
from tools.catalog import ModelToolCatalog
from tools.registry import ToolNotFoundError
from tools.service import ToolArgumentsError, ToolExecutionService


_MAX_MODEL_REQUESTS_PER_TURN = 3
_MAX_TOOL_CALLS_PER_TURN = 4
_MAX_TOOL_MESSAGE_CONTENT_CHARS = 12_000
_TRUNCATED_TOOL_RESULT = {"truncated": True}


class ModelToolLimitError(ModelGatewayError):
    def __init__(
        self,
        code: str,
        attempts: list[ModelAttempt],
    ) -> None:
        self.code = code
        self.attempts = tuple(attempts)
        self.public_error = None
        super().__init__("model tool limit reached")


class ModelToolOrchestrationError(ModelGatewayError):
    def __init__(
        self,
        *,
        error: ModelGatewayError,
        attempts: list[ModelAttempt],
    ) -> None:
        self.code = error.code
        self.attempts = tuple(attempts)
        self.public_error = error
        super().__init__("model orchestration failed")


class ModelToolOrchestrator:
    def __init__(
        self,
        *,
        gateway: LanguageModelGateway,
        catalog: ModelToolCatalog,
        tool_service: ToolExecutionService | None,
        enabled: bool,
    ) -> None:
        self.gateway = gateway
        self.catalog = catalog
        self.tool_service = tool_service
        self.enabled = enabled

    async def run(
        self,
        request: ModelRequest,
        *,
        source: MessageSource,
    ) -> ModelOrchestrationResult:
        messages = list(request.messages)
        tools = list(self.catalog.list(source)) if self.enabled else []
        advertised_tool_names = {tool.name for tool in tools}
        attempts: list[ModelAttempt] = []
        seen_call_ids: set[str] = set()
        tool_call_count = 0

        for model_round in range(_MAX_MODEL_REQUESTS_PER_TURN):
            current = request.model_copy(
                update={"messages": list(messages), "tools": tools}
            )
            reply, attempt = await self._complete(
                current,
                completed_attempts=attempts,
            )
            attempts.append(attempt)
            if not reply.tool_calls:
                return ModelOrchestrationResult(
                    reply=reply,
                    attempts=attempts,
                )

            calls = reply.tool_calls
            if model_round == _MAX_MODEL_REQUESTS_PER_TURN - 1:
                raise ModelToolLimitError(
                    "model_tool_round_limit",
                    attempts,
                )
            if tool_call_count + len(calls) > _MAX_TOOL_CALLS_PER_TURN:
                raise ModelToolLimitError(
                    "model_tool_call_limit",
                    attempts,
                )
            call_ids = [call.id for call in calls]
            if (
                len(set(call_ids)) != len(call_ids)
                or any(call_id in seen_call_ids for call_id in call_ids)
            ):
                error = ModelProtocolError("duplicate tool call id")
                raise ModelToolOrchestrationError(
                    error=error,
                    attempts=attempts,
                ) from error

            messages.append(
                ModelMessage(
                    role=ModelRole.ASSISTANT,
                    content=None,
                    reasoning_content=reply.reasoning_content,
                    tool_calls=calls,
                )
            )
            for call_index, call in enumerate(calls):
                seen_call_ids.add(call.id)
                tool_call_count += 1
                result = await self._execute_tool(
                    request,
                    call,
                    model_round,
                    call_index,
                    advertised_tool_names,
                )
                messages.append(self._tool_message(result))

        raise ModelToolLimitError(
            "model_tool_round_limit",
            attempts,
        )

    async def _complete(
        self,
        request: ModelRequest,
        *,
        completed_attempts: list[ModelAttempt],
    ) -> tuple[ModelReply, ModelAttempt]:
        started_at = perf_counter()
        try:
            reply = await self.gateway.complete(request)
        except ModelGatewayError as error:
            failed_attempt = ModelAttempt(
                model=self.gateway.model_name,
                status=error.code,
                latency_ms=self._elapsed_ms(started_at),
            )
            raise ModelToolOrchestrationError(
                error=error,
                attempts=[*completed_attempts, failed_attempt],
            ) from error

        return reply, ModelAttempt(
            model=reply.model,
            status="succeeded",
            latency_ms=self._elapsed_ms(started_at),
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            provider_request_id=reply.provider_request_id,
        )

    async def _execute_tool(
        self,
        request: ModelRequest,
        call: ModelToolCall,
        model_round: int,
        call_index: int,
        advertised_tool_names: set[str],
    ) -> ModelToolResult:
        if (
            call.name not in advertised_tool_names
            or self.tool_service is None
        ):
            return self._failed_tool_result(
                call,
                "tool_not_available",
            )

        material = (
            f"{request.correlation_id}\0{model_round}\0"
            f"{call_index}\0{call.id}"
        ).encode("utf-8")
        correlation_id = f"model:{sha256(material).hexdigest()}"
        tool_request = ToolRequest(
            correlation_id=correlation_id,
            source=ToolSource.MODEL,
            tool_name=call.name,
            arguments=call.arguments,
        )
        try:
            view = await self.tool_service.request(tool_request)
        except ToolNotFoundError:
            return self._failed_tool_result(
                call,
                "tool_not_available",
            )
        except ToolArgumentsError:
            return self._failed_tool_result(
                call,
                "tool_arguments_invalid",
            )
        return self._result_from_view(call, view)

    @staticmethod
    def _result_from_view(
        call: ModelToolCall,
        view: ToolRequestView,
    ) -> ModelToolResult:
        return ModelToolResult(
            call_id=call.id,
            name=call.name,
            state=view.state.value,
            result=view.result,
            error_code=view.error_code,
        )

    @staticmethod
    def _failed_tool_result(
        call: ModelToolCall,
        error_code: str,
    ) -> ModelToolResult:
        return ModelToolResult(
            call_id=call.id,
            name=call.name,
            state=ToolRequestState.FAILED.value,
            error_code=error_code,
        )

    @staticmethod
    def _tool_message(result: ModelToolResult) -> ModelMessage:
        payload = result.model_dump(mode="json")
        content = ModelToolOrchestrator._compact_json(payload)
        if len(content) > _MAX_TOOL_MESSAGE_CONTENT_CHARS:
            payload["result"] = _TRUNCATED_TOOL_RESULT
            content = ModelToolOrchestrator._compact_json(payload)
        return ModelMessage(
            role=ModelRole.TOOL,
            content=content,
            tool_call_id=result.call_id,
            name=result.name,
        )

    @staticmethod
    def _compact_json(payload: dict) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, int((perf_counter() - started_at) * 1000))
