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
from llm.models import (
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelToolCall,
    ModelToolDefinition,
)
from llm.openai_compatible import OpenAICompatibleGateway


def _settings(
    *,
    enabled=True,
    base_url="https://llm.example/v1",
    api_key="test-api-key",
    model="chat-model",
    timeout_seconds=17,
    tool_calling_enabled=False,
):
    return LLMSettings(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        model=model,
        tool_calling_enabled=tool_calling_enabled,
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


def _request_with_time_tool():
    return ModelRequest(
        correlation_id="correlation-id",
        messages=[ModelMessage(role=ModelRole.USER, content="几点了")],
        tools=[
            ModelToolDefinition(
                name="system.current_time",
                description="读取当前时间",
                parameters={
                    "type": "object",
                    "properties": {
                        "timezone": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
            )
        ],
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

    async def test_complete_serializes_tools_and_tool_messages(self):
        captured_payload = None

        def handler(request):
            nonlocal captured_payload
            captured_payload = json.loads(request.content)
            return _json_response(
                {
                    "choices": [
                        {
                            "message": {"content": "现在是 12:00"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(handler),
        )
        tool = ModelToolDefinition(
            name="system.current_time",
            description="读取当前时间",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        call = ModelToolCall(
            id="call-1",
            name=tool.name,
            arguments={"timezone": "UTC"},
        )
        request = ModelRequest(
            correlation_id="message-1",
            tools=[tool],
            messages=[
                ModelMessage(role=ModelRole.USER, content="几点了"),
                ModelMessage(
                    role=ModelRole.ASSISTANT,
                    content="这段附带文本不应进入历史请求",
                    tool_calls=[call],
                    reasoning_content="private-reasoning-sentinel",
                ),
                ModelMessage(
                    role=ModelRole.TOOL,
                    content='{"state":"succeeded"}',
                    tool_call_id=call.id,
                    name=call.name,
                ),
            ],
        )

        await gateway.complete(request)

        self.assertEqual(
            captured_payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "system_current_time_2a9c83b2",
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            ],
        )
        self.assertNotIn("tool_choice", captured_payload)
        self.assertEqual(
            captured_payload["messages"][1],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "system_current_time_2a9c83b2",
                            "arguments": '{"timezone":"UTC"}',
                        },
                    }
                ],
                "reasoning_content": "private-reasoning-sentinel",
            },
        )
        self.assertEqual(
            captured_payload["messages"][2],
            {
                "role": "tool",
                "content": '{"state":"succeeded"}',
                "tool_call_id": "call-1",
                "name": "system_current_time_2a9c83b2",
            },
        )

    async def test_complete_keeps_legal_short_tool_name(self):
        captured_payload = None

        def handler(request):
            nonlocal captured_payload
            captured_payload = json.loads(request.content)
            return _json_response(
                {"choices": [{"message": {"content": "done"}}]}
            )

        tool = ModelToolDefinition(
            name="current_time",
            description="读取当前时间",
            parameters={"type": "object", "properties": {}},
        )
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(handler),
        )

        await gateway.complete(
            ModelRequest(
                correlation_id="message-1",
                messages=[
                    ModelMessage(role=ModelRole.USER, content="几点了")
                ],
                tools=[tool],
            )
        )

        self.assertEqual(
            captured_payload["tools"][0]["function"]["name"],
            "current_time",
        )

    async def test_complete_generates_unique_bounded_tool_aliases(self):
        captured_payload = None

        def handler(request):
            nonlocal captured_payload
            captured_payload = json.loads(request.content)
            return _json_response(
                {"choices": [{"message": {"content": "done"}}]}
            )

        names = [
            "system.current_time",
            "system_current_time_2a9c83b2",
            "memory." + "x" * 80,
        ]
        tools = [
            ModelToolDefinition(
                name=name,
                description=f"工具 {index}",
                parameters={"type": "object", "properties": {}},
            )
            for index, name in enumerate(names)
        ]
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(handler),
        )

        await gateway.complete(
            ModelRequest(
                correlation_id="message-1",
                messages=[
                    ModelMessage(role=ModelRole.USER, content="运行工具")
                ],
                tools=tools,
            )
        )

        aliases = [
            item["function"]["name"]
            for item in captured_payload["tools"]
        ]
        self.assertEqual(len(set(aliases)), len(names))
        self.assertEqual(aliases[1], "system_current_time_2a9c83b2")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertRegex(alias, r"^[A-Za-z0-9_-]{1,64}$")
                self.assertLessEqual(len(alias), 64)

    async def test_complete_rejects_duplicate_internal_tool_names_locally(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return _json_response(
                {"choices": [{"message": {"content": "done"}}]}
            )

        tool = _request_with_time_tool().tools[0]
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(handler),
        )
        request = ModelRequest(
            correlation_id="message-1",
            messages=[ModelMessage(role=ModelRole.USER, content="几点了")],
            tools=[tool, tool],
        )

        with self.assertRaises(ModelConfigurationError):
            await gateway.complete(request)

        self.assertEqual(calls, 0)

    async def test_complete_rejects_undeclared_message_tools_locally(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return _json_response(
                {"choices": [{"message": {"content": "done"}}]}
            )

        declared = _request_with_time_tool().tools[0]
        undeclared_call = ModelToolCall(
            id="call-1",
            name="memory.search",
            arguments={},
        )
        invalid_message_sets = (
            [
                ModelMessage(role=ModelRole.USER, content="搜索"),
                ModelMessage(
                    role=ModelRole.ASSISTANT,
                    tool_calls=[undeclared_call],
                ),
            ],
            [
                ModelMessage(role=ModelRole.USER, content="搜索"),
                ModelMessage(
                    role=ModelRole.TOOL,
                    content='{"state":"succeeded"}',
                    tool_call_id="call-1",
                    name="memory.search",
                ),
            ],
        )
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(handler),
        )

        for messages in invalid_message_sets:
            with self.subTest(role=messages[-1].role):
                with self.assertRaises(ModelConfigurationError):
                    await gateway.complete(
                        ModelRequest(
                            correlation_id="message-1",
                            messages=messages,
                            tools=[declared],
                        )
                    )

        self.assertEqual(calls, 0)

    async def test_complete_parses_tool_calls_with_optional_text(self):
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "model": "provider-model",
                        "choices": [
                            {
                                "message": {
                                    "content": "先读取时间",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": (
                                                    "system_current_time_"
                                                    "2a9c83b2"
                                                ),
                                                "arguments": '{"timezone":"UTC"}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                )
            ),
        )

        reply = await gateway.complete(_request_with_time_tool())

        self.assertEqual(reply.text, "先读取时间")
        self.assertEqual(
            reply.tool_calls,
            [
                ModelToolCall(
                    id="call-1",
                    name="system.current_time",
                    arguments={"timezone": "UTC"},
                )
            ],
        )
        self.assertEqual(reply.model, "provider-model")

    async def test_complete_parses_tool_calls_without_text(self):
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": (
                                                    "system_current_time_"
                                                    "2a9c83b2"
                                                ),
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )
            ),
        )

        reply = await gateway.complete(_request_with_time_tool())

        self.assertIsNone(reply.text)
        self.assertEqual(reply.tool_calls[0].arguments, {})

    async def test_complete_maps_response_alias_and_keeps_tool_reasoning(self):
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "reasoning_content": (
                                        "private-reasoning-sentinel"
                                    ),
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": (
                                                    "system_current_time_"
                                                    "2a9c83b2"
                                                ),
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )
            ),
        )

        reply = await gateway.complete(_request_with_time_tool())

        self.assertEqual(reply.tool_calls[0].name, "system.current_time")
        self.assertEqual(
            reply.reasoning_content,
            "private-reasoning-sentinel",
        )

    async def test_complete_discards_reasoning_from_final_text_reply(self):
        gateway = OpenAICompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "done",
                                    "reasoning_content": (
                                        "private-reasoning-sentinel"
                                    ),
                                }
                            }
                        ]
                    }
                )
            ),
        )

        reply = await gateway.complete(_request())

        self.assertEqual(reply.text, "done")
        self.assertIsNone(reply.reasoning_content)

    async def test_complete_rejects_unknown_provider_tool_alias(self):
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "unknown_tool",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                )
            ),
        )

        with self.assertRaises(ModelProtocolError):
            await gateway.complete(_request_with_time_tool())

    async def test_complete_rejects_invalid_reasoning_content(self):
        invalid_values = ("   ", "x" * 64_001, {"private": "value"})
        for invalid_value in invalid_values:
            with self.subTest(value_type=type(invalid_value).__name__):
                gateway = OpenAICompatibleGateway(
                    _settings(tool_calling_enabled=True),
                    transport=httpx.MockTransport(
                        lambda request, value=invalid_value: _json_response(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "content": None,
                                            "reasoning_content": value,
                                            "tool_calls": [
                                                {
                                                    "id": "call-1",
                                                    "type": "function",
                                                    "function": {
                                                        "name": (
                                                            "system_current_"
                                                            "time_2a9c83b2"
                                                        ),
                                                        "arguments": "{}",
                                                    },
                                                }
                                            ],
                                        }
                                    }
                                ]
                            }
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError) as raised:
                    await gateway.complete(_request_with_time_tool())

                self.assertEqual(
                    str(raised.exception),
                    "model service returned an invalid response",
                )

    async def test_complete_rejects_tool_calls_when_request_has_no_tools(self):
        for content in (None, "意外附带文本"):
            with self.subTest(content=content):
                gateway = OpenAICompatibleGateway(
                    _settings(tool_calling_enabled=True),
                    transport=httpx.MockTransport(
                        lambda request, value=content: _json_response(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "content": value,
                                            "tool_calls": [
                                                {
                                                    "id": "unexpected-call",
                                                    "type": "function",
                                                    "function": {
                                                        "name": (
                                                            "system.current_time"
                                                        ),
                                                        "arguments": "{}",
                                                    },
                                                }
                                            ],
                                        }
                                    }
                                ]
                            }
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError):
                    await gateway.complete(_request())

    async def test_complete_rejects_malformed_tool_call_envelopes(self):
        invalid_calls = (
            {
                "id": "call-1",
                "type": "other",
                "function": {
                    "name": "system.current_time",
                    "arguments": "{}",
                },
            },
            {
                "id": "call-1",
                "function": {
                    "name": "system.current_time",
                    "arguments": "{}",
                },
            },
            {
                "id": "",
                "type": "function",
                "function": {
                    "name": "system.current_time",
                    "arguments": "{}",
                },
            },
            {
                "id": "   ",
                "type": "function",
                "function": {
                    "name": "system.current_time",
                    "arguments": "{}",
                },
            },
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "", "arguments": "{}"},
            },
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "   ", "arguments": "{}"},
            },
        )

        for tool_call in invalid_calls:
            with self.subTest(tool_call=tool_call):
                gateway = OpenAICompatibleGateway(
                    _settings(tool_calling_enabled=True),
                    transport=httpx.MockTransport(
                        lambda request, call=tool_call: _json_response(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "content": None,
                                            "tool_calls": [call],
                                        }
                                    }
                                ]
                            }
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError):
                    await gateway.complete(_request_with_time_tool())

    async def test_complete_rejects_oversized_or_non_object_tool_arguments(self):
        invalid_arguments = (
            "[]",
            '"UTC"',
            "{",
            '{"value":NaN}',
            json.dumps({"value": "你" * 6000}, ensure_ascii=False),
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments[:20]):
                gateway = OpenAICompatibleGateway(
                    _settings(tool_calling_enabled=True),
                    transport=httpx.MockTransport(
                        lambda request, value=arguments: _json_response(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "content": None,
                                            "tool_calls": [
                                                {
                                                    "id": "call-1",
                                                    "type": "function",
                                                    "function": {
                                                        "name": (
                                                            "system.current_time"
                                                        ),
                                                        "arguments": value,
                                                    },
                                                }
                                            ],
                                        }
                                    }
                                ]
                            }
                        )
                    ),
                )

                with self.assertRaises(ModelProtocolError):
                    await gateway.complete(_request_with_time_tool())

    async def test_complete_rejects_duplicate_tool_call_ids(self):
        tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "system.current_time",
                    "arguments": "{}",
                },
            },
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "system.current_time",
                    "arguments": '{"timezone":"UTC"}',
                },
            },
        ]
        gateway = OpenAICompatibleGateway(
            _settings(tool_calling_enabled=True),
            transport=httpx.MockTransport(
                lambda request: _json_response(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": tool_calls,
                                }
                            }
                        ]
                    }
                )
            ),
        )

        with self.assertRaises(ModelProtocolError):
            await gateway.complete(_request_with_time_tool())

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
