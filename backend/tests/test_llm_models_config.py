import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm import (
    LanguageModelGateway,
    ModelAttempt,
    ModelMessage,
    ModelOrchestrationResult,
    ModelReply,
    ModelRequest,
    ModelRole,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolResult,
)
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway


class LLMSettingsTests(unittest.TestCase):
    def test_disabled_mode_does_not_require_remote_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = LLMSettings.from_env()

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.tool_calling_enabled)
        self.assertIsNone(settings.base_url)
        self.assertIsNone(settings.api_key)
        self.assertIsNone(settings.model)
        self.assertEqual(settings.timeout_seconds, 60)
        self.assertEqual(settings.max_context_messages, 20)
        self.assertEqual(settings.max_context_chars, 12000)

    def test_enabled_mode_requires_base_url(self):
        environment = {
            "ASSISTANT_LLM_ENABLED": "true",
            "ASSISTANT_LLM_MODEL": "model-name",
        }

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "ASSISTANT_LLM_BASE_URL"):
                LLMSettings.from_env()

    def test_enabled_mode_requires_model(self):
        environment = {
            "ASSISTANT_LLM_ENABLED": "true",
            "ASSISTANT_LLM_BASE_URL": "https://llm.example/v1",
        }

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "ASSISTANT_LLM_MODEL"):
                LLMSettings.from_env()

    def test_environment_values_are_normalized(self):
        environment = {
            "ASSISTANT_LLM_ENABLED": "  YeS  ",
            "ASSISTANT_LLM_BASE_URL": "  https://llm.example/v1///  ",
            "ASSISTANT_LLM_API_KEY": "  secret-value  ",
            "ASSISTANT_LLM_MODEL": "  model-name  ",
            "ASSISTANT_LLM_TIMEOUT_SECONDS": " 30 ",
            "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES": " 12 ",
            "ASSISTANT_LLM_MAX_CONTEXT_CHARS": " 8000 ",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = LLMSettings.from_env()

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.base_url, "https://llm.example/v1")
        self.assertEqual(settings.api_key, "secret-value")
        self.assertEqual(settings.model, "model-name")
        self.assertEqual(settings.timeout_seconds, 30)
        self.assertEqual(settings.max_context_messages, 12)
        self.assertEqual(settings.max_context_chars, 8000)

    def test_blank_optional_strings_become_none(self):
        environment = {
            "ASSISTANT_LLM_BASE_URL": " /// ",
            "ASSISTANT_LLM_API_KEY": "   ",
            "ASSISTANT_LLM_MODEL": "   ",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = LLMSettings.from_env()

        self.assertIsNone(settings.base_url)
        self.assertIsNone(settings.api_key)
        self.assertIsNone(settings.model)

    def test_boolean_parser_rejects_unknown_values(self):
        for value in ("enabled", "2", ""):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"ASSISTANT_LLM_ENABLED": value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "ASSISTANT_LLM_ENABLED",
                    ):
                        LLMSettings.from_env()

    def test_boolean_parser_accepts_all_documented_values(self):
        for value in ("1", "true", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                environment = {
                    "ASSISTANT_LLM_ENABLED": value,
                    "ASSISTANT_LLM_BASE_URL": "https://llm.example/v1",
                    "ASSISTANT_LLM_MODEL": "model-name",
                }
                with patch.dict(os.environ, environment, clear=True):
                    self.assertTrue(LLMSettings.from_env().enabled)

        for value in ("0", "false", "no", "off", " OFF "):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"ASSISTANT_LLM_ENABLED": value},
                    clear=True,
                ):
                    self.assertFalse(LLMSettings.from_env().enabled)

    def test_tool_calling_boolean_defaults_to_false_and_accepts_yes(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(LLMSettings.from_env().tool_calling_enabled)

        with patch.dict(
            os.environ,
            {"ASSISTANT_LLM_TOOL_CALLING_ENABLED": " yes "},
            clear=True,
        ):
            self.assertTrue(LLMSettings.from_env().tool_calling_enabled)

    def test_tool_calling_boolean_rejects_unknown_values(self):
        with patch.dict(
            os.environ,
            {"ASSISTANT_LLM_TOOL_CALLING_ENABLED": "sometimes"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "ASSISTANT_LLM_TOOL_CALLING_ENABLED",
            ):
                LLMSettings.from_env()

    def test_numeric_values_must_be_in_range(self):
        cases = (
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", "0"),
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", "301"),
            ("ASSISTANT_LLM_MAX_CONTEXT_MESSAGES", "0"),
            ("ASSISTANT_LLM_MAX_CONTEXT_MESSAGES", "101"),
            ("ASSISTANT_LLM_MAX_CONTEXT_CHARS", "3999"),
            ("ASSISTANT_LLM_MAX_CONTEXT_CHARS", "100001"),
        )

        for name, value in cases:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaisesRegex(ValueError, name):
                        LLMSettings.from_env()

    def test_numeric_values_accept_inclusive_endpoints(self):
        cases = (
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", "timeout_seconds", 1),
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", "timeout_seconds", 300),
            (
                "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES",
                "max_context_messages",
                1,
            ),
            (
                "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES",
                "max_context_messages",
                100,
            ),
            ("ASSISTANT_LLM_MAX_CONTEXT_CHARS", "max_context_chars", 4000),
            (
                "ASSISTANT_LLM_MAX_CONTEXT_CHARS",
                "max_context_chars",
                100000,
            ),
        )

        for name, field, value in cases:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: str(value)}, clear=True):
                    settings = LLMSettings.from_env()

                self.assertEqual(getattr(settings, field), value)


class ModelContractTests(unittest.TestCase):
    def test_roles_have_provider_neutral_wire_values(self):
        self.assertEqual(ModelRole.SYSTEM.value, "system")
        self.assertEqual(ModelRole.USER.value, "user")
        self.assertEqual(ModelRole.ASSISTANT.value, "assistant")
        self.assertEqual(ModelRole.TOOL.value, "tool")

    def test_provider_neutral_tool_contracts_are_valid_and_frozen(self):
        definition = ModelToolDefinition(
            name="memory.search",
            description="Search memories",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        call = ModelToolCall(
            id="call-1",
            name="memory.search",
            arguments={"query": "hello"},
        )
        result = ModelToolResult(
            call_id="call-1",
            name="memory.search",
            state="success",
            result={"items": []},
            error_code=None,
        )
        reply = ModelReply(text="done", model="model")
        attempt = ModelAttempt(
            model="model",
            status="success",
            latency_ms=12,
            prompt_tokens=3,
            completion_tokens=2,
            provider_request_id="request-1",
        )
        orchestration = ModelOrchestrationResult(
            reply=reply,
            attempts=[attempt],
        )

        self.assertEqual(definition.parameters["type"], "object")
        self.assertEqual(call.arguments["query"], "hello")
        self.assertEqual(result.call_id, "call-1")
        self.assertEqual(orchestration.attempts[0].latency_ms, 12)

        for model in (definition, call, result, attempt, orchestration):
            with self.subTest(model=type(model).__name__):
                with self.assertRaises(ValidationError):
                    type(model)(**{**model.model_dump(), "unexpected": True})
                with self.assertRaises(ValidationError):
                    model.__setattr__(next(iter(type(model).model_fields)), "changed")

    def test_existing_public_models_remain_mutable_and_ignore_extra_fields(self):
        message = ModelMessage(
            role=ModelRole.USER,
            content="hello",
            provider_extension=True,
        )
        request = ModelRequest(
            correlation_id="c",
            messages=[message],
            provider_extension=True,
        )
        reply = ModelReply(
            text="answer",
            model="model",
            provider_extension=True,
        )

        message.content = "updated"
        request.temperature = 0.5
        reply.text = "updated"

        self.assertEqual(message.content, "updated")
        self.assertEqual(request.temperature, 0.5)
        self.assertEqual(reply.text, "updated")

    def test_tool_definition_description_accepts_1000_and_rejects_1001(self):
        definition = ModelToolDefinition(
            name="memory.search",
            description="d" * 1000,
            parameters={"type": "object"},
        )
        self.assertEqual(len(definition.description), 1000)

        with self.assertRaises(ValidationError):
            ModelToolDefinition(
                name="memory.search",
                description="d" * 1001,
                parameters={"type": "object"},
            )

    def test_tool_result_requires_object_result_when_present(self):
        result = ModelToolResult(
            call_id="call-1",
            name="memory.search",
            state="success",
            result={"items": []},
        )
        self.assertEqual(result.result, {"items": []})

        with self.assertRaises(ValidationError):
            ModelToolResult(
                call_id="call-1",
                name="memory.search",
                state="success",
                result="not-an-object",
            )

    def test_orchestration_result_accepts_three_attempts_and_rejects_four(self):
        attempt = ModelAttempt(
            model="model",
            status="success",
            latency_ms=12,
        )
        reply = ModelReply(text="done", model="model")
        result = ModelOrchestrationResult(
            reply=reply,
            attempts=[attempt] * 3,
        )
        self.assertEqual(len(result.attempts), 3)

        with self.assertRaises(ValidationError):
            ModelOrchestrationResult(
                reply=reply,
                attempts=[attempt] * 4,
            )

    def test_tool_schema_requires_top_level_object(self):
        with self.assertRaises(ValidationError):
            ModelToolDefinition(
                name="memory.search",
                description="Search memories",
                parameters={"type": "array", "items": {"type": "string"}},
            )

    def test_message_role_shapes_are_enforced(self):
        call = ModelToolCall(
            id="call-1",
            name="memory.search",
            arguments={"query": "hello"},
        )
        valid_messages = (
            ModelMessage(role=ModelRole.SYSTEM, content="system"),
            ModelMessage(role=ModelRole.USER, content="user"),
            ModelMessage(role=ModelRole.ASSISTANT, content="answer"),
            ModelMessage(role=ModelRole.ASSISTANT, tool_calls=[call]),
            ModelMessage(
                role=ModelRole.TOOL,
                content='{"items":[]}',
                tool_call_id="call-1",
                name="memory.search",
            ),
        )
        self.assertEqual(len(valid_messages), 5)

        invalid_messages = (
            {"role": ModelRole.SYSTEM, "content": None},
            {"role": ModelRole.USER, "content": "user", "tool_calls": [call]},
            {"role": ModelRole.ASSISTANT, "content": None},
            {
                "role": ModelRole.ASSISTANT,
                "content": "answer",
                "tool_call_id": "call-1",
            },
            {"role": ModelRole.TOOL, "content": "result", "name": "memory.search"},
            {
                "role": ModelRole.TOOL,
                "content": "result",
                "tool_call_id": "call-1",
                "name": "memory.search",
                "tool_calls": [call],
            },
        )
        for values in invalid_messages:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ModelMessage(**values)

    def test_message_tool_field_boundaries_are_enforced(self):
        call = ModelToolCall(
            id="c",
            name="abc",
            arguments={},
        )
        ModelMessage(role=ModelRole.ASSISTANT, tool_calls=[call] * 4)
        ModelMessage(
            role=ModelRole.TOOL,
            content="result",
            tool_call_id="c" * 200,
            name="abc",
        )

        invalid_messages = (
            {"role": ModelRole.ASSISTANT, "tool_calls": [call] * 5},
            {
                "role": ModelRole.TOOL,
                "content": "result",
                "tool_call_id": "c" * 201,
                "name": "abc",
            },
            {
                "role": ModelRole.TOOL,
                "content": "result",
                "tool_call_id": "c",
                "name": "Invalid Name",
            },
        )
        for values in invalid_messages:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ModelMessage(**values)

    def test_request_accepts_inclusive_boundaries(self):
        request = ModelRequest(
            correlation_id="c" * 200,
            messages=[ModelMessage(role=ModelRole.USER, content="x" * 12000)],
            temperature=2,
            max_output_tokens=8192,
        )

        self.assertEqual(len(request.messages[0].content), 12000)
        self.assertEqual(request.temperature, 2)
        self.assertEqual(request.max_output_tokens, 8192)

    def test_request_accepts_minimum_boundaries(self):
        request = ModelRequest(
            correlation_id="c",
            messages=[ModelMessage(role=ModelRole.USER, content="x")],
            temperature=0,
            max_output_tokens=1,
        )

        self.assertEqual(request.correlation_id, "c")
        self.assertEqual(request.messages[0].content, "x")
        self.assertEqual(request.temperature, 0)
        self.assertEqual(request.max_output_tokens, 1)
        self.assertEqual(request.tools, [])

    def test_request_accepts_up_to_32_tools(self):
        tool = ModelToolDefinition(
            name="abc",
            description="A tool",
            parameters={"type": "object"},
        )
        request = ModelRequest(
            correlation_id="c",
            messages=[ModelMessage(role=ModelRole.USER, content="x")],
            tools=[tool] * 32,
        )
        self.assertEqual(len(request.tools), 32)

        with self.assertRaises(ValidationError):
            ModelRequest(
                correlation_id="c",
                messages=[ModelMessage(role=ModelRole.USER, content="x")],
                tools=[tool] * 33,
            )

    def test_request_rejects_invalid_boundaries(self):
        valid_message = ModelMessage(role=ModelRole.USER, content="hello")
        invalid_requests = (
            {"correlation_id": "", "messages": [valid_message]},
            {"correlation_id": "c" * 201, "messages": [valid_message]},
            {"correlation_id": "id", "messages": []},
            {
                "correlation_id": "id",
                "messages": [ModelMessage(role=ModelRole.USER, content="hello")],
                "temperature": -0.01,
            },
            {
                "correlation_id": "id",
                "messages": [valid_message],
                "temperature": 2.01,
            },
            {
                "correlation_id": "id",
                "messages": [valid_message],
                "max_output_tokens": 0,
            },
            {
                "correlation_id": "id",
                "messages": [valid_message],
                "max_output_tokens": 8193,
            },
        )

        for values in invalid_requests:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ModelRequest(**values)

    def test_message_rejects_content_outside_bounds(self):
        for content in ("", "x" * 12001):
            with self.subTest(length=len(content)):
                with self.assertRaises(ValidationError):
                    ModelMessage(role=ModelRole.USER, content=content)

    def test_reply_accepts_inclusive_boundaries(self):
        reply = ModelReply(
            text="x" * 4000,
            model="m" * 200,
            finish_reason="f" * 100,
            prompt_tokens=0,
            completion_tokens=0,
            provider_request_id="r" * 300,
        )

        self.assertEqual(len(reply.text), 4000)
        self.assertEqual(reply.prompt_tokens, 0)
        self.assertEqual(reply.completion_tokens, 0)

    def test_reply_accepts_minimum_required_fields(self):
        reply = ModelReply(text="x", model="m")

        self.assertEqual(reply.text, "x")
        self.assertEqual(reply.model, "m")
        self.assertIsNone(reply.finish_reason)
        self.assertIsNone(reply.prompt_tokens)
        self.assertIsNone(reply.completion_tokens)
        self.assertIsNone(reply.provider_request_id)
        self.assertEqual(reply.tool_calls, [])

    def test_reply_requires_text_or_tool_calls_and_allows_both(self):
        call = ModelToolCall(
            id="call-1",
            name="memory.search",
            arguments={"query": "hello"},
        )

        with self.assertRaises(ValidationError):
            ModelReply(model="model")

        text_only = ModelReply(text="answer", model="model")
        tool_only = ModelReply(tool_calls=[call], model="model")
        both = ModelReply(text="checking", tool_calls=[call], model="model")

        self.assertEqual(text_only.text, "answer")
        self.assertIsNone(tool_only.text)
        self.assertEqual(len(tool_only.tool_calls), 1)
        self.assertEqual(both.text, "checking")

    def test_reply_rejects_invalid_boundaries(self):
        invalid_replies = (
            {"text": "", "model": "model"},
            {"text": "x" * 4001, "model": "model"},
            {"text": "reply", "model": ""},
            {"text": "reply", "model": "m" * 201},
            {"text": "reply", "model": "model", "finish_reason": "f" * 101},
            {"text": "reply", "model": "model", "prompt_tokens": -1},
            {"text": "reply", "model": "model", "completion_tokens": -1},
            {
                "text": "reply",
                "model": "model",
                "provider_request_id": "r" * 301,
            },
        )

        for values in invalid_replies:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ModelReply(**values)


class DemoLanguageModelGatewayTests(unittest.TestCase):
    def test_demo_gateway_satisfies_contract_and_returns_fixed_reply(self):
        gateway = DemoLanguageModelGateway()
        request = ModelRequest(
            correlation_id="message-1",
            messages=[ModelMessage(role=ModelRole.USER, content="你好")],
        )

        reply = asyncio.run(gateway.complete(request))

        self.assertIsInstance(gateway, LanguageModelGateway)
        self.assertEqual(gateway.model_name, "demo")
        self.assertEqual(reply.text, "主人说得有道理~")
        self.assertEqual(reply.model, "demo")
        self.assertEqual(reply.finish_reason, "stop")


if __name__ == "__main__":
    unittest.main()
