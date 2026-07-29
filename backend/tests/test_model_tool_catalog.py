import sys
import unittest
from pathlib import Path

from pydantic import BaseModel, RootModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.tools import ToolRisk, ToolSource
from tools.catalog import ModelToolCatalog
from tools.registry import ToolDefinition, ToolRegistry


class Arguments(BaseModel):
    value: str


class ScalarArguments(RootModel[str]):
    pass


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
    )


class ModelToolCatalogTests(unittest.TestCase):
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

        tools = ModelToolCatalog(registry).list()

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

    def test_catalog_rejects_non_object_argument_schema(self):
        registry = ToolRegistry()
        registry.register(
            definition(
                "example.scalar",
                allowed_sources=frozenset({ToolSource.MODEL}),
                arguments_model=ScalarArguments,
            )
        )

        with self.assertRaises(ValueError):
            ModelToolCatalog(registry).list()


if __name__ == "__main__":
    unittest.main()
