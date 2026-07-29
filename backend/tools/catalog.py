from collections.abc import Sequence
from copy import deepcopy

from domain.tools import ToolRisk, ToolSource
from llm.models import ModelToolDefinition
from tools.registry import ToolRegistry


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
            parameters = deepcopy(
                definition.arguments_model.model_json_schema()
            )
            if parameters.get("type") != "object":
                raise ValueError(
                    "tool parameters must be a top-level object schema"
                )
            parameters["additionalProperties"] = False
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
