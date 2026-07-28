from typing import Any

import httpx
from pydantic import BaseModel, StrictInt, StrictStr, ValidationError

from .config import LLMSettings
from .errors import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServiceError,
    ModelTimeoutError,
)
from .models import ModelReply, ModelRequest


class _ResponseMessage(BaseModel):
    content: StrictStr


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
                message.model_dump(mode="json", include={"role", "content"})
                for message in request.messages
            ],
            "stream": False,
        }
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
            text = choice.message.content.strip()
            if not text:
                raise ValueError("empty completion content")

            response_model = (completion.model or "").strip()
            usage = completion.usage
            return ModelReply(
                text=text,
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
