from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from pydantic.errors import (
    PydanticInvalidForJsonSchema,
    PydanticSchemaGenerationError,
)
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from computer.models import ComputerPlatform, ModelAccess
from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from llm.models import ModelToolDefinition
from tools.registry import ToolDefinition, ToolRegistry


class _ClosedModelJsonSchema(GenerateJsonSchema):
    def model_schema(
        self,
        schema: core_schema.ModelSchema,
    ) -> JsonSchemaValue:
        generated = super().model_schema(schema)
        if not schema.get("root_model"):
            generated["additionalProperties"] = False
        return generated


class _UnsupportedToolSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class ModelToolCallContext:
    channel: MessageSource
    advertised_tool_names: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.channel, MessageSource):
            raise TypeError("model tool channel must be a MessageSource")
        if not isinstance(self.advertised_tool_names, frozenset) or any(
            not isinstance(name, str) for name in self.advertised_tool_names
        ):
            raise TypeError("advertised model tools must be a frozenset of names")


class ModelToolPolicy:
    def __init__(
        self,
        *,
        platform: ComputerPlatform | None,
        runtime_profile: Literal["desktop", "cloud"] | None,
        confirmation_client_online: Callable[[], bool],
        allowed_tool_names: frozenset[str] | None,
    ) -> None:
        self.platform = platform
        self.runtime_profile = runtime_profile
        self.confirmation_client_online = confirmation_client_online
        self.allowed_tool_names = allowed_tool_names

    def allows(
        self,
        definition: ToolDefinition,
        channel: MessageSource,
        risk: ToolRisk,
    ) -> bool:
        if (
            not isinstance(channel, MessageSource)
            or self.runtime_profile not in {"desktop", "cloud"}
            or ToolSource.MODEL not in definition.allowed_sources
            or channel not in definition.allowed_channels
            or (
                self.allowed_tool_names is not None
                and definition.name not in self.allowed_tool_names
            )
        ):
            return False
        if self.runtime_profile == "cloud" and (
            channel is not MessageSource.QQ
            or self.allowed_tool_names is None
        ):
            return False
        if risk is ToolRisk.LOW:
            return definition.model_access is ModelAccess.READ_ONLY
        if (
            risk is not ToolRisk.HIGH
            or definition.model_access
            is not ModelAccess.PROPOSE_WITH_CONFIRMATION
            or channel is not MessageSource.DESKTOP
            or self.platform is not ComputerPlatform.MACOS
            or self.runtime_profile != "desktop"
        ):
            return False
        try:
            return self.confirmation_client_online() is True
        except Exception:
            return False


def build_closed_arguments_schema(
    definition: ToolDefinition,
) -> dict[str, Any]:
    schema = deepcopy(
        definition.arguments_model.model_json_schema(
            schema_generator=_ClosedModelJsonSchema,
        )
    )
    if schema.get("type") != "object":
        raise _UnsupportedToolSchemaError(
            "tool parameters must be a top-level object schema"
        )
    _close_object_schemas(schema)
    return schema


def reject_additional_arguments(
    arguments: Any,
    validated_arguments: BaseModel,
) -> None:
    _reject_additional_properties(arguments, validated_arguments)


def _close_object_schemas(value: Any) -> None:
    if isinstance(value, dict):
        if (
            value.get("type") == "object"
            and "additionalProperties" not in value
        ):
            value["additionalProperties"] = False
        for child in value.values():
            _close_object_schemas(child)
    elif isinstance(value, list):
        for child in value:
            _close_object_schemas(child)


def _reject_additional_properties(
    value: Any,
    validated_value: Any,
) -> None:
    if isinstance(validated_value, BaseModel):
        if type(validated_value).__pydantic_root_model__:
            _reject_additional_properties(
                value,
                validated_value.root,
            )
            return
        if not isinstance(value, dict):
            return
        consumed_fields: set[str] = set()
        for key, child in value.items():
            field_name = _field_name_for_input(validated_value, key)
            if (
                field_name is None
                or field_name not in validated_value.model_fields_set
                or field_name in consumed_fields
            ):
                raise ValueError(
                    "tool arguments contain additional properties"
                )
            consumed_fields.add(field_name)
            _reject_additional_properties(
                child,
                getattr(validated_value, field_name),
            )
        return

    if isinstance(validated_value, Mapping) and isinstance(value, dict):
        if len(value) != len(validated_value):
            raise ValueError("tool argument mapping was normalized ambiguously")
        for child, validated_child in zip(
            value.values(),
            validated_value.values(),
            strict=True,
        ):
            _reject_additional_properties(child, validated_child)
        return

    if (
        isinstance(validated_value, (list, tuple))
        and isinstance(value, list)
    ):
        if len(value) != len(validated_value):
            raise ValueError("tool argument sequence was normalized ambiguously")
        for child, validated_child in zip(
            value,
            validated_value,
            strict=True,
        ):
            _reject_additional_properties(child, validated_child)


def _field_name_for_input(
    model: BaseModel,
    input_name: str,
) -> str | None:
    for field_name, field in type(model).model_fields.items():
        if input_name == field_name:
            return field_name
        if input_name == field.alias:
            return field_name
        validation_alias = field.validation_alias
        if input_name == validation_alias:
            return field_name
        for choice in getattr(validation_alias, "choices", ()):
            if input_name == choice:
                return field_name
    return None


class ModelToolCatalog:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        platform: ComputerPlatform | None = None,
        runtime_profile: Literal["desktop", "cloud"] = "desktop",
        confirmation_client_online: Callable[[], bool] | None = None,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> None:
        if platform is not None and not isinstance(
            platform,
            ComputerPlatform,
        ):
            raise TypeError("model tool platform must be a ComputerPlatform")
        if runtime_profile not in {"desktop", "cloud"}:
            raise ValueError("model tool runtime profile is invalid")
        if (
            confirmation_client_online is not None
            and not callable(confirmation_client_online)
        ):
            raise TypeError("confirmation client status must be callable")
        self.registry = registry
        self.platform = platform
        self.runtime_profile = runtime_profile
        self.confirmation_client_online = (
            confirmation_client_online or (lambda: False)
        )
        self.allowed_tool_names = allowed_tool_names
        self.policy = ModelToolPolicy(
            platform=platform,
            runtime_profile=runtime_profile,
            confirmation_client_online=self.confirmation_client_online,
            allowed_tool_names=allowed_tool_names,
        )

    def list(
        self,
        source: MessageSource,
    ) -> Sequence[ModelToolDefinition]:
        if not isinstance(source, MessageSource):
            raise TypeError("model tool source must be a MessageSource")
        tools: list[ModelToolDefinition] = []
        for definition in self.registry.list():
            if not self.policy.allows(
                definition,
                source,
                definition.risk,
            ):
                continue
            try:
                parameters = build_closed_arguments_schema(definition)
                tool = ModelToolDefinition(
                    name=definition.name,
                    description=f"{definition.title}。{definition.impact}",
                    parameters=parameters,
                )
            except (
                _UnsupportedToolSchemaError,
                PydanticInvalidForJsonSchema,
                PydanticSchemaGenerationError,
                ValidationError,
            ):
                continue
            tools.append(tool)
        return tuple(tools)
