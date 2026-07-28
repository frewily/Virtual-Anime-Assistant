import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.config import LLMSettings
from llm.errors import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelGatewayError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServiceError,
    ModelTimeoutError,
)
from llm.models import ModelMessage, ModelRequest, ModelRole
from llm.openai_compatible import OpenAICompatibleGateway


def _settings(
    *,
    enabled=True,
    base_url="https://llm.example/v1",
    api_key="test-api-key",
    model="chat-model",
    timeout_seconds=17,
):
    return LLMSettings(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_context_messages=20,
        max_context_chars=12000,
    )


def _request(*, temperature=None, max_output_tokens=None):
    return ModelRequest(
        correlation_id="correlation-id",
        messages=[
            ModelMessage(role=ModelRole.SYSTEM, content="Be concise."),
            ModelMessage(role=ModelRole.USER, content="Hello"),
        ],
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def _json_response(payload, *, status_code=200, headers=None):
    return httpx.Response(
        status_code,
        headers=headers,
        json=payload,
    )


class OpenAICompatibleGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_posts_exact_basic_request_and_reads_reply_metadata(self):
        captured_request = None

        def handler(request):
            nonlocal captured_request
            captured_request = request
            return _json_response(
                {
                    "model": "provider-model",
                    "choices": [
                        {
                            "message": {"content": "  Hello back  "},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                    },
                },
                headers={"x-request-id": "provider-request-123"},
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )

        reply = await gateway.complete(_request())

        self.assertEqual(
            captured_request.url,
            httpx.URL("https://llm.example/v1/chat/completions"),
        )
        self.assertEqual(captured_request.headers["content-type"], "application/json")
        self.assertEqual(
            captured_request.headers["authorization"],
            "Bearer test-api-key",
        )
        self.assertEqual(
            json.loads(captured_request.content),
            {
                "model": "chat-model",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Hello"},
                ],
                "stream": False,
            },
        )
        self.assertNotIn("tools", json.loads(captured_request.content))
        self.assertNotIn("tool_choice", json.loads(captured_request.content))
        self.assertEqual(reply.text, "Hello back")
        self.assertEqual(reply.model, "provider-model")
        self.assertEqual(reply.finish_reason, "stop")
        self.assertEqual(reply.prompt_tokens, 12)
        self.assertEqual(reply.completion_tokens, 3)
        self.assertEqual(reply.provider_request_id, "provider-request-123")

    async def test_complete_omits_authorization_when_api_key_is_blank(self):
        captured_headers = None

        def handler(request):
            nonlocal captured_headers
            captured_headers = request.headers
            return _json_response(
                {"choices": [{"message": {"content": "reply"}}]}
            )

        gateway = OpenAICompatibleGateway(
            _settings(api_key="   "),
            transport=httpx.MockTransport(handler),
        )

        await gateway.complete(_request())

        self.assertNotIn("authorization", captured_headers)

    async def test_complete_includes_optional_generation_fields(self):
        captured_payload = None

        def handler(request):
            nonlocal captured_payload
            captured_payload = json.loads(request.content)
            return _json_response(
                {"choices": [{"message": {"content": "reply"}}]}
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )

        await gateway.complete(_request(temperature=0.4, max_output_tokens=256))

        self.assertEqual(captured_payload["temperature"], 0.4)
        self.assertEqual(captured_payload["max_tokens"], 256)

    async def test_complete_allows_missing_usage_and_uses_configured_model(self):
        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {"content": "reply"},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            ),
        )

        reply = await gateway.complete(_request())

        self.assertEqual(reply.model, "chat-model")
        self.assertIsNone(reply.prompt_tokens)
        self.assertIsNone(reply.completion_tokens)

    async def test_http_errors_are_normalized_without_response_body(self):
        cases = (
            (401, ModelAuthenticationError),
            (403, ModelAuthenticationError),
            (429, ModelRateLimitError),
            (500, ModelServiceError),
        )

        for status_code, error_type in cases:
            with self.subTest(status_code=status_code):
                private_body = f"private-body-{status_code}"
                gateway = OpenAICompatibleGateway(
                    _settings(),
                    transport=httpx.MockTransport(
                        lambda request, code=status_code, body=private_body: (
                            httpx.Response(code, text=body)
                        )
                    ),
                )

                with self.assertRaises(error_type) as context:
                    await gateway.complete(_request())

                self.assertIn(str(status_code), str(context.exception))
                self.assertNotIn(private_body, str(context.exception))
                self.assertNotIn("test-api-key", str(context.exception))

    async def test_connection_error_is_normalized_without_sensitive_details(self):
        sensitive_url = "https://llm.example/v1/chat/completions?token=url-secret"

        def handler(request):
            raise httpx.ConnectError(
                f"could not connect to {sensitive_url} with test-api-key",
                request=request,
            )

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaises(ModelServiceError) as context:
            await gateway.complete(_request())

        message = str(context.exception)
        self.assertNotIn("url-secret", message)
        self.assertNotIn("test-api-key", message)
        self.assertNotIn("?token=", message)

    async def test_timeout_is_normalized(self):
        def handler(request):
            raise httpx.ReadTimeout("provider took too long", request=request)

        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaises(ModelTimeoutError):
            await gateway.complete(_request())

    async def test_non_json_success_response_is_protocol_error(self):
        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="not-json")
            ),
        )

        with self.assertRaises(ModelProtocolError):
            await gateway.complete(_request())

    async def test_missing_or_empty_choices_are_protocol_errors(self):
        for payload in ({}, {"choices": []}):
            with self.subTest(payload=payload):
                gateway = OpenAICompatibleGateway(
                    _settings(),
                    transport=httpx.MockTransport(
                        lambda request, response_payload=payload: _json_response(
                            response_payload
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError):
                    await gateway.complete(_request())

    async def test_empty_or_non_string_content_is_protocol_error(self):
        for content in ("", "   ", 123, None):
            with self.subTest(content=content):
                gateway = OpenAICompatibleGateway(
                    _settings(),
                    transport=httpx.MockTransport(
                        lambda request, value=content: _json_response(
                            {"choices": [{"message": {"content": value}}]}
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError):
                    await gateway.complete(_request())

    async def test_response_validation_failure_is_protocol_error(self):
        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "model": "m" * 201,
                        "choices": [{"message": {"content": "reply"}}],
                    }
                )
            ),
        )

        with self.assertRaises(ModelProtocolError):
            await gateway.complete(_request())

    def test_disabled_or_incomplete_configuration_is_rejected(self):
        cases = (
            _settings(enabled=False),
            _settings(base_url=None),
            _settings(base_url="   "),
            _settings(model=None),
            _settings(model="   "),
        )

        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ModelConfigurationError) as context:
                    OpenAICompatibleGateway(settings)

                self.assertNotIn("test-api-key", str(context.exception))

    def test_model_name_returns_validated_configured_model(self):
        gateway = OpenAICompatibleGateway(_settings(model="  chat-model  "))

        self.assertEqual(gateway.model_name, "chat-model")

    def test_gateway_error_codes_are_stable(self):
        cases = (
            (ModelGatewayError, "service_error"),
            (ModelConfigurationError, "configuration_error"),
            (ModelAuthenticationError, "authentication_error"),
            (ModelRateLimitError, "rate_limit_error"),
            (ModelTimeoutError, "timeout_error"),
            (ModelProtocolError, "protocol_error"),
            (ModelServiceError, "service_error"),
        )

        for error_type, expected_code in cases:
            with self.subTest(error_type=error_type):
                self.assertTrue(issubclass(error_type, ModelGatewayError))
                self.assertEqual(error_type.code, expected_code)


if __name__ == "__main__":
    unittest.main()
