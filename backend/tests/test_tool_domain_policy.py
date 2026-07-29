import sys
import unittest
from pathlib import Path

from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.tools import ToolRequest, ToolRisk, ToolSource
from tools.policy import ToolPolicy, summarize_arguments
from tools.registry import ToolDefinition, ToolNotFoundError, ToolRegistry


class SecretArguments(BaseModel):
    target: str
    token: str


async def read_example(arguments: SecretArguments) -> dict:
    return {"target": arguments.target}


def definition(
    *,
    name: str = "example.read",
    risk: ToolRisk = ToolRisk.LOW,
    allowed_sources: frozenset[ToolSource] | None = None,
) -> ToolDefinition:
    values = dict(
        name=name,
        title="读取示例",
        arguments_model=SecretArguments,
        risk=risk,
        impact="只读取示例状态",
        timeout_seconds=2,
        cancellable=True,
        sensitive_fields=frozenset({"token"}),
        handler=read_example,
    )
    if allowed_sources is not None:
        values["allowed_sources"] = allowed_sources
    return ToolDefinition(**values)


class ToolDomainPolicyTests(unittest.TestCase):
    def test_definition_defaults_to_desktop_and_system_sources(self):
        self.assertEqual(
            definition().allowed_sources,
            frozenset({ToolSource.DESKTOP, ToolSource.SYSTEM}),
        )

    def test_definition_rejects_empty_or_invalid_allowed_sources(self):
        with self.assertRaises(ValueError):
            definition(allowed_sources=frozenset())
        with self.assertRaises(TypeError):
            definition(allowed_sources=frozenset({ToolSource.DESKTOP, "model"}))

    def test_registry_rejects_duplicates_and_resolves_stably(self):
        registry = ToolRegistry()
        registered = definition()

        registry.register(registered)

        self.assertIs(registry.require("example.read"), registered)
        self.assertEqual(registry.list(), (registered,))
        with self.assertRaises(ValueError):
            registry.register(registered)
        with self.assertRaises(ToolNotFoundError):
            registry.require("example.missing")

    def test_definition_rejects_invalid_contracts(self):
        invalid_values = (
            {"name": "INVALID NAME"},
            {"title": " "},
            {"impact": ""},
            {"timeout_seconds": 0},
        )

        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                ToolDefinition(
                    **{
                        **definition().__dict__,
                        **changes,
                    }
                )

    def test_request_cannot_override_locally_computed_risk(self):
        registered = definition(risk=ToolRisk.HIGH)
        request = ToolRequest(
            correlation_id="message-1",
            source=ToolSource.DESKTOP,
            tool_name=registered.name,
            arguments={"target": "demo", "token": "private"},
        )

        self.assertEqual(
            ToolPolicy().risk_for(registered, request.arguments),
            ToolRisk.HIGH,
        )
        self.assertFalse(hasattr(request, "risk"))
        with self.assertRaises(ValidationError):
            ToolRequest(
                correlation_id="message-1",
                source=ToolSource.DESKTOP,
                tool_name=registered.name,
                arguments={},
                risk="low",
            )

    def test_request_requires_a_safe_name_and_correlation_id(self):
        for values in (
            {"correlation_id": "", "tool_name": "example.read"},
            {"correlation_id": "message-1", "tool_name": "../shell"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ToolRequest(
                    source=ToolSource.DESKTOP,
                    arguments={},
                    **values,
                )

    def test_argument_summary_redacts_and_limits_untrusted_values(self):
        summary = summarize_arguments(
            {
                "target": "x" * 300,
                "TOKEN": "private",
                "nested": {"token": "also-private"},
                "items": list(range(30)),
            },
            frozenset({"token"}),
        )

        self.assertEqual(summary["TOKEN"], "[REDACTED]")
        self.assertEqual(summary["nested"]["token"], "[REDACTED]")
        self.assertEqual(len(summary["target"]), 201)
        self.assertTrue(summary["target"].endswith("…"))
        self.assertEqual(len(summary["items"]), 21)
        self.assertEqual(summary["items"][-1], "[TRUNCATED]")

    def test_argument_summary_limits_depth_and_mapping_size(self):
        summary = summarize_arguments(
            {
                "deep": {"a": {"b": {"c": {"d": {"e": "secret"}}}}},
                "many": {str(index): index for index in range(25)},
            },
            frozenset(),
        )

        self.assertEqual(
            summary["deep"]["a"]["b"]["c"],
            "[TRUNCATED]",
        )
        self.assertEqual(len(summary["many"]), 21)
        self.assertTrue(summary["many"]["_truncated"])


if __name__ == "__main__":
    unittest.main()
