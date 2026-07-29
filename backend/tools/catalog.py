from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from domain.tools import ToolRisk, ToolSource
from llm.models import ModelToolDefinition
from tools.registry import ToolDefinition, ToolRegistry


def build_closed_arguments_schema(
    definition: ToolDefinition,
) -> dict[str, Any]:
    schema = deepcopy(definition.arguments_model.model_json_schema())
    if schema.get("type") != "object":
        raise ValueError(
            "tool parameters must be a top-level object schema"
        )
    _close_object_schemas(schema)
    return schema


def reject_additional_arguments(
    arguments: Any,
    schema: dict[str, Any],
) -> None:
    _reject_additional_properties(arguments, schema, schema)


def _close_object_schemas(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            value["additionalProperties"] = False
        for child in value.values():
            _close_object_schemas(child)
    elif isinstance(value, list):
        for child in value:
            _close_object_schemas(child)


def _reject_additional_properties(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        _reject_additional_properties(
            value,
            _resolve_local_reference(root_schema, reference),
            root_schema,
        )

    for keyword in ("allOf",):
        for branch in schema.get(keyword, ()):
            if isinstance(branch, dict):
                _reject_additional_properties(
                    value,
                    branch,
                    root_schema,
                )

    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(alternatives, list):
        matching = [
            branch
            for branch in alternatives
            if isinstance(branch, dict)
            and _matches_json_shape(value, branch, root_schema)
        ]
        failures: list[ValueError] = []
        for branch in matching:
            try:
                _reject_additional_properties(
                    value,
                    branch,
                    root_schema,
                )
                break
            except ValueError as exc:
                failures.append(exc)
        else:
            if failures:
                raise failures[0]

    if schema.get("type") == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        if any(key not in properties for key in value):
            raise ValueError("tool arguments contain additional properties")
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _reject_additional_properties(
                    child,
                    child_schema,
                    root_schema,
                )
    elif schema.get("type") == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                _reject_additional_properties(
                    item,
                    item_schema,
                    root_schema,
                )


def _resolve_local_reference(
    root_schema: dict[str, Any],
    reference: str,
) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError("tool schema contains an unsupported reference")
    resolved: Any = root_schema
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(resolved, dict) or key not in resolved:
            raise ValueError("tool schema reference was not found")
        resolved = resolved[key]
    if not isinstance(resolved, dict):
        raise ValueError("tool schema reference is invalid")
    return resolved


def _matches_json_shape(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
) -> bool:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return _matches_json_shape(
            value,
            _resolve_local_reference(root_schema, reference),
            root_schema,
        )
    expected = schema.get("type")
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


class ModelToolCatalog:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def list(self) -> Sequence[ModelToolDefinition]:
        tools: list[ModelToolDefinition] = []
        for definition in self.registry.list():
            if (
                definition.risk is not ToolRisk.LOW
                or ToolSource.MODEL not in definition.allowed_sources
            ):
                continue
            parameters = build_closed_arguments_schema(definition)
            tools.append(
                ModelToolDefinition(
                    name=definition.name,
                    description=(
                        f"{definition.title}。{definition.impact}"
                    ),
                    parameters=parameters,
                )
            )
        return tuple(tools)
