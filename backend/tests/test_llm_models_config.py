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
    ModelMessage,
    ModelReply,
    ModelRequest,
    ModelRole,
)
from llm.config import LLMSettings
from llm.demo import DemoLanguageModelGateway


class LLMSettingsTests(unittest.TestCase):
    def test_disabled_mode_does_not_require_remote_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = LLMSettings.from_env()

        self.assertFalse(settings.enabled)
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
