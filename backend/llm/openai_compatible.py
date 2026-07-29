import json
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
)


def _reject_non_json_constant(_: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


class _ResponseFunction(BaseModel):
    name: StrictStr
    arguments: StrictStr


class _ResponseToolCall(BaseModel):
    id: StrictStr
    type: Literal["function"]
    function: _ResponseFunction


class _ResponseMessage(BaseModel):
    content: StrictStr | None = None
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

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete(self, request: ModelRequest) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": [
                self._message_payload(message) for message in request.messages
            ],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
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
        return self._parse_reply(response)

    @staticmethod
    def _message_payload(message: ModelMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role.value,
            "content": message.content,
        }
        if message.role is ModelRole.ASSISTANT and message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
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
        elif message.role is ModelRole.TOOL:
            payload["tool_call_id"] = message.tool_call_id
            payload["name"] = message.name
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

    def _parse_reply(self, response: httpx.Response) -> ModelReply:
        try:
            completion = _ChatCompletionResponse.model_validate(response.json())
            if not completion.choices:
                raise ValueError("missing completion choice")

            choice = completion.choices[0]
            content = choice.message.content
            text = content.strip() if content is not None else None
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
                        name=function.name,
                        arguments=arguments,
                    )
                )

            if text is None and not tool_calls:
                raise ValueError("empty completion content")

            response_model = (completion.model or "").strip()
            usage = completion.usage
            return ModelReply(
                text=text,
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
