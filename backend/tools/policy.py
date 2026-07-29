from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

from domain.tools import ToolRisk
from tools.registry import ToolDefinition


_MAX_DEPTH = 4
_MAX_ITEMS = 20
_MAX_STRING_LENGTH = 200
_REDACTED = "[REDACTED]"
_TRUNCATED = "[TRUNCATED]"


class ToolPolicy:
    def risk_for(
        self,
        definition: ToolDefinition,
        _: Mapping[str, Any],
    ) -> ToolRisk:
        return definition.risk


def summarize_arguments(
    arguments: Mapping[str, Any],
    sensitive_fields: frozenset[str],
) -> dict[str, Any]:
    sensitive = {
        field_name.casefold()
        for field_name in sensitive_fields
    }
    return _summarize_mapping(arguments, sensitive, depth=0)


def _summarize_mapping(
    mapping: Mapping[Any, Any],
    sensitive: set[str],
    *,
    depth: int,
) -> dict[str, Any]:
    if depth >= _MAX_DEPTH:
        return {"_value": _TRUNCATED}

    items = list(mapping.items())
    summary: dict[str, Any] = {}
    for key, value in items[:_MAX_ITEMS]:
        normalized_key = str(key)
        summary[normalized_key] = (
            _REDACTED
            if normalized_key.casefold() in sensitive
            else _summarize_value(value, sensitive, depth=depth + 1)
        )
    if len(items) > _MAX_ITEMS:
        summary["_truncated"] = True
    return summary


def _summarize_value(
    value: Any,
    sensitive: set[str],
    *,
    depth: int,
) -> Any:
    if depth >= _MAX_DEPTH:
        return _TRUNCATED
    if isinstance(value, BaseModel):
        return _summarize_mapping(
            value.model_dump(mode="json"),
            sensitive,
            depth=depth,
        )
    if isinstance(value, Mapping):
        return _summarize_mapping(value, sensitive, depth=depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        summary = [
            _summarize_value(item, sensitive, depth=depth + 1)
            for item in items[:_MAX_ITEMS]
        ]
        if len(items) > _MAX_ITEMS:
            summary.append(_TRUNCATED)
        return summary
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return value[:_MAX_STRING_LENGTH] + "…"
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)
