import sys
import unittest
from pathlib import Path

from pydantic import BaseModel, ConfigDict, RootModel
from pydantic.errors import PydanticInvalidForJsonSchema

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from tools.catalog import ModelToolCatalog
from tools.registry import ToolDefinition, ToolRegistry


class Arguments(BaseModel):
    value: str


class NestedArguments(BaseModel):
    label: str


class ArgumentsWithNestedModel(BaseModel):
    nested: NestedArguments


class NamedMapping(RootModel[dict[str, NestedArguments]]):
    pass


class MappingArguments(BaseModel):
    direct: dict[str, NestedArguments]
    arrays: list[dict[str, NestedArguments]]
    referenced: NamedMapping


class ExtraAllowedNestedArguments(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str


class ExtraAllowedArguments(BaseModel):
    model_config = ConfigDict(extra="allow")

    nested: ExtraAllowedNestedArguments


class ScalarArguments(RootModel[str]):
    pass


class InvalidJsonSchemaArguments(BaseModel):
    value: str

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        raise PydanticInvalidForJsonSchema("unsupported test schema")


class ProgrammingErrorArguments(BaseModel):
    value: str

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        raise RuntimeError("programming error")


SHARED_SCHEMA = {
    "type": "object",
    "properties": {
        "nested": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
        }
    },
}


class SharedSchemaArguments(BaseModel):
    nested: NestedArguments

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        return SHARED_SCHEMA


async def handler(_: BaseModel) -> dict:
    return {}


def definition(
    name: str,
    *,
    risk: ToolRisk = ToolRisk.LOW,
    allowed_sources: frozenset[ToolSource] = frozenset(
        {ToolSource.DESKTOP, ToolSource.SYSTEM}
    ),
    arguments_model: type[BaseModel] = Arguments,
    allowed_channels: frozenset[MessageSource] = frozenset(
        {MessageSource.DESKTOP, MessageSource.QQ}
    ),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        title=f"{name} 标题",
        arguments_model=arguments_model,
        risk=risk,
        impact=f"{name} 影响",
        timeout_seconds=1,
        cancellable=True,
        handler=handler,
        allowed_sources=allowed_sources,
        allowed_channels=allowed_channels,
    )


class ModelToolCatalogTests(unittest.TestCase):
    def test_catalog_filters_tools_by_trusted_message_source(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.desktop",
                allowed_sources=frozenset({ToolSource.MODEL}),
                allowed_channels=frozenset({MessageSource.DESKTOP}),
            )
        )
        registry.register(
            definition(
                "example.qq",
                allowed_sources=frozenset({ToolSource.MODEL}),
                allowed_channels=frozenset({MessageSource.QQ}),
            )
        )

        catalog = ModelToolCatalog(registry)

        self.assertEqual(
            [tool.name for tool in catalog.list(MessageSource.DESKTOP)],
            ["example.desktop"],
        )
        self.assertEqual(
            [tool.name for tool in catalog.list(MessageSource.QQ)],
            ["example.qq"],
        )
    def test_catalog_exports_only_model_authorized_low_risk_tools_stably(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.zeta",
                allowed_sources=frozenset({ToolSource.MODEL}),
            )
        )
        registry.register(definition("example.hidden"))
        registry.register(
            definition(
                "example.danger",
                risk=ToolRisk.HIGH,
                allowed_sources=frozenset({ToolSource.MODEL}),
            )
        )
        registry.register(
            definition(
                "example.alpha",
                allowed_sources=frozenset(
                    {ToolSource.DESKTOP, ToolSource.MODEL}
                ),
            )
        )

        tools = ModelToolCatalog(registry).list(MessageSource.DESKTOP)

        self.assertEqual(
            [tool.name for tool in tools],
            ["example.alpha", "example.zeta"],
        )
        self.assertEqual(
            tools[0].description,
            "example.alpha 标题。example.alpha 影响",
        )
        self.assertEqual(tools[0].parameters["type"], "object")
        self.assertIs(
            tools[0].parameters["additionalProperties"],
            False,
        )

    def test_catalog_filters_non_object_argument_schema(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.scalar",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=ScalarArguments,
            )
        )

        self.assertEqual(ModelToolCatalog(registry).list(MessageSource.DESKTOP), ())

    def test_catalog_filters_expected_schema_failures_and_keeps_valid_tools(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.schema-error",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=InvalidJsonSchemaArguments,
            )
        )
        registry.register(
            definition(
                "example.bad-description",
                allowed_sources=frozenset({ToolSource.MODEL}),
            )
        )
        registry.register(
            definition(
                "example.valid",
                allowed_sources=frozenset({ToolSource.MODEL}),
            )
        )
        bad_description = registry.require("example.bad-description")
        object.__setattr__(bad_description, "title", "x" * 1001)

        tools = ModelToolCatalog(registry).list(MessageSource.DESKTOP)

        self.assertEqual([tool.name for tool in tools], ["example.valid"])

    def test_catalog_does_not_swallow_programming_errors(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.programming-error",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=ProgrammingErrorArguments,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            ModelToolCatalog(registry).list(MessageSource.DESKTOP)

    def test_catalog_closes_top_level_and_nested_ref_object_schemas(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.nested",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=ArgumentsWithNestedModel,
            )
        )

        parameters = ModelToolCatalog(registry).list(
            MessageSource.DESKTOP
        )[0].parameters

        self.assertIs(parameters["additionalProperties"], False)
        self.assertIs(
            parameters["$defs"]["NestedArguments"][
                "additionalProperties"
            ],
            False,
        )

    def test_catalog_deep_copies_schema_before_closing_objects(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.shared",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=SharedSchemaArguments,
            )
        )

        parameters = ModelToolCatalog(registry).list(
            MessageSource.DESKTOP
        )[0].parameters

        self.assertNotIn("additionalProperties", SHARED_SCHEMA)
        self.assertNotIn(
            "additionalProperties",
            SHARED_SCHEMA["properties"]["nested"],
        )
        self.assertIs(parameters["additionalProperties"], False)
        self.assertIs(
            parameters["properties"]["nested"]["additionalProperties"],
            False,
        )

    def test_catalog_preserves_and_closes_mapping_value_schemas(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.mapping",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=MappingArguments,
            )
        )

        parameters = ModelToolCatalog(registry).list(
            MessageSource.DESKTOP
        )[0].parameters

        item_reference = {"$ref": "#/$defs/NestedArguments"}
        self.assertEqual(
            parameters["properties"]["direct"]["additionalProperties"],
            item_reference,
        )
        self.assertEqual(
            parameters["properties"]["arrays"]["items"][
                "additionalProperties"
            ],
            item_reference,
        )
        self.assertEqual(
            parameters["$defs"]["NamedMapping"][
                "additionalProperties"
            ],
            item_reference,
        )
        self.assertIs(
            parameters["$defs"]["NestedArguments"][
                "additionalProperties"
            ],
            False,
        )

    def test_catalog_closes_top_level_and_nested_extra_allow_models(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.extra-allow",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=ExtraAllowedArguments,
            )
        )

        parameters = ModelToolCatalog(registry).list(
            MessageSource.DESKTOP
        )[0].parameters

        self.assertIs(parameters["additionalProperties"], False)
        self.assertIs(
            parameters["$defs"]["ExtraAllowedNestedArguments"][
                "additionalProperties"
            ],
            False,
        )


if __name__ == "__main__":
    unittest.main()
