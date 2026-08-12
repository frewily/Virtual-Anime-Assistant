import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, NoReturn

import httpx
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError

from .config import LLMSettings
from .errors import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServiceError,
    ModelTimeoutError,
)
from .models import (
    ModelMessage,
    ModelReply,
    ModelRequest,
    ModelRole,
    ModelToolCall,
    ModelToolDefinition,
)


_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INVALID_PROVIDER_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_REASONING_CONTENT_CHARS = 64_000


def _reject_non_json_constant(_: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


@dataclass(frozen=True)
class _ToolNameAliases:
    internal_to_provider: dict[str, str]
    provider_to_internal: dict[str, str]

    @classmethod
    def build(
        cls,
        tools: list[ModelToolDefinition],
    ) -> "_ToolNameAliases":
        internal_names = [tool.name for tool in tools]
        if len(set(internal_names)) != len(internal_names):
            raise ModelConfigurationError("duplicate model tool name")

        used = {
            name
            for name in internal_names
            if _PROVIDER_TOOL_NAME.fullmatch(name)
        }
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for index, internal_name in enumerate(internal_names, start=1):
            if _PROVIDER_TOOL_NAME.fullmatch(internal_name):
                candidate = internal_name
            else:
                cleaned = _INVALID_PROVIDER_TOOL_NAME.sub("_", internal_name)
                cleaned = cleaned.strip("_") or "tool"
                digest = sha256(internal_name.encode("utf-8")).hexdigest()[:8]
                for offset in range(len(tools) + 1):
                    suffix = (
                        f"_{digest}"
                        if offset == 0
                        else f"_{index + offset - 1}_{digest}"
                    )
                    candidate = f"{cleaned[:64 - len(suffix)]}{suffix}"
                    if candidate not in used:
                        break
                else:
                    raise ModelConfigurationError(
                        "model tool aliases are not unique"
                    )
            forward[internal_name] = candidate
            reverse[candidate] = internal_name
            used.add(candidate)
        return cls(forward, reverse)

    def to_provider(self, internal_name: str) -> str:
        try:
            return self.internal_to_provider[internal_name]
        except KeyError:
            raise ModelConfigurationError(
                "model message references an undeclared tool"
            ) from None

    def to_internal(self, provider_name: str) -> str:
        try:
            return self.provider_to_internal[provider_name]
        except KeyError:
            raise ModelProtocolError(
                "model service returned an invalid response"
            ) from None


class _ResponseFunction(BaseModel):
    name: StrictStr
    arguments: StrictStr


class _ResponseToolCall(BaseModel):
    id: StrictStr
    type: Literal["function"]
    function: _ResponseFunction


class _ResponseMessage(BaseModel):
    content: StrictStr | None = None
    reasoning_content: StrictStr | None = None
    tool_calls: list[_ResponseToolCall] = Field(default_factory=list)


class _ResponseChoice(BaseModel):
    message: _ResponseMessage
    finish_reason: StrictStr | None = None


class _ResponseUsage(BaseModel):
    prompt_tokens: StrictInt | None = None
    completion_tokens: StrictInt | None = None


class _ChatCompletionResponse(BaseModel):
    choices: list[_ResponseChoice]
    model: StrictStr | None = None
    usage: _ResponseUsage | None = None


class OpenAICompatibleGateway:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        accept_reasoning_only: bool = False,
    ) -> None:
        base_url = (settings.base_url or "").strip().rstrip("/")
        model_name = (settings.model or "").strip()
        if not settings.enabled or not base_url or not model_name:
            raise ModelConfigurationError("model gateway configuration is incomplete")

        self._base_url = base_url
        self._api_key = (settings.api_key or "").strip()
        self._model_name = model_name
        self._timeout_seconds = settings.timeout_seconds
        self._transport = transport
        self._accept_reasoning_only = accept_reasoning_only

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete(self, request: ModelRequest) -> ModelReply:
        aliases = _ToolNameAliases.build(request.tools)
        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": [
                self._message_payload(message, aliases)
                for message in request.messages
            ],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": aliases.to_provider(tool.name),
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException:
            raise ModelTimeoutError("model service request timed out") from None
        except httpx.RequestError:
            raise ModelServiceError("model service request failed") from None

        self._raise_for_status(response.status_code)
        return self._parse_reply(
            response,
            allow_tool_calls=bool(request.tools),
            aliases=aliases,
        )

    @staticmethod
    def _message_payload(
        message: ModelMessage,
        aliases: _ToolNameAliases,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role.value,
            "content": message.content,
        }
        if message.role is ModelRole.ASSISTANT and message.tool_calls:
            payload["content"] = None
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": aliases.to_provider(call.name),
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                }
                for call in message.tool_calls
            ]
            if message.reasoning_content is not None:
                payload["reasoning_content"] = message.reasoning_content
            return payload
        if message.role is ModelRole.TOOL:
            payload["tool_call_id"] = message.tool_call_id
            payload["name"] = aliases.to_provider(message.name)
        return payload

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code < 400:
            return
        if status_code in (401, 403):
            raise ModelAuthenticationError(
                f"model service returned HTTP {status_code}"
            )
        if status_code == 429:
            raise ModelRateLimitError(f"model service returned HTTP {status_code}")
        raise ModelServiceError(f"model service returned HTTP {status_code}")

    def _parse_reply(
        self,
        response: httpx.Response,
        *,
        allow_tool_calls: bool,
        aliases: _ToolNameAliases,
    ) -> ModelReply:
        try:
            completion = _ChatCompletionResponse.model_validate(response.json())
            if not completion.choices:
                raise ValueError("missing completion choice")

            choice = completion.choices[0]
            if choice.message.tool_calls and not allow_tool_calls:
                raise ValueError("unexpected tool calls")
            reasoning_content = choice.message.reasoning_content
            if choice.message.tool_calls and reasoning_content is not None and (
                not reasoning_content.strip()
                or len(reasoning_content) > _MAX_REASONING_CONTENT_CHARS
            ):
                raise ValueError("invalid reasoning content")
            content = choice.message.content
            text = content.strip() if content is not None else None
            if text == "":
                text = None
            if text is None and self._accept_reasoning_only:
                text = (
                    reasoning_content.strip()
                    if reasoning_content is not None
                    else None
                )
                if text == "":
                    text = None

            tool_calls: list[ModelToolCall] = []
            call_ids: set[str] = set()
            for response_call in choice.message.tool_calls:
                call_id = response_call.id
                function = response_call.function
                if not call_id.strip() or not function.name.strip():
                    raise ValueError("empty tool call id or name")
                if call_id in call_ids:
                    raise ValueError("duplicate tool call id")
                call_ids.add(call_id)

                raw_arguments = function.arguments
                if len(raw_arguments.encode("utf-8")) > 16 * 1024:
                    raise ValueError("tool arguments exceed size limit")
                arguments = json.loads(
                    raw_arguments,
                    parse_constant=_reject_non_json_constant,
                )
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                tool_calls.append(
                    ModelToolCall(
                        id=call_id,
                        name=aliases.to_internal(function.name),
                        arguments=arguments,
                    )
                )

            if text is None and not tool_calls:
                raise ValueError("empty completion content")

            response_model = (completion.model or "").strip()
            usage = completion.usage
            return ModelReply(
                text=text,
                reasoning_content=(
                    reasoning_content if tool_calls else None
                ),
                tool_calls=tool_calls,
                model=response_model or self._model_name,
                finish_reason=choice.finish_reason,
                prompt_tokens=usage.prompt_tokens if usage is not None else None,
                completion_tokens=(
                    usage.completion_tokens if usage is not None else None
                ),
                provider_request_id=response.headers.get("x-request-id"),
            )
        except (ValidationError, ValueError, TypeError, IndexError):
            raise ModelProtocolError(
                "model service returned an invalid response"
            ) from None
