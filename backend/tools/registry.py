import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource


_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,99}$")
ToolHandler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


class ToolNotFoundError(LookupError):
    def __init__(self, tool_name: str) -> None:
        super().__init__("registered tool was not found")
        self.tool_name = tool_name


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    arguments_model: type[BaseModel]
    risk: ToolRisk
    impact: str
    timeout_seconds: float
    cancellable: bool
    handler: ToolHandler
    sensitive_fields: frozenset[str] = field(default_factory=frozenset)
    allowed_sources: frozenset[ToolSource] = field(
        default_factory=lambda: frozenset(
            {ToolSource.DESKTOP, ToolSource.SYSTEM}
        )
    )
    allowed_channels: frozenset[MessageSource] = field(
        default_factory=lambda: frozenset(
            {MessageSource.DESKTOP, MessageSource.QQ}
        )
    )

    def __post_init__(self) -> None:
        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("tool name is invalid")
        if not self.title.strip():
            raise ValueError("tool title must not be blank")
        if not self.impact.strip():
            raise ValueError("tool impact must not be blank")
        if not isinstance(self.arguments_model, type) or not issubclass(
            self.arguments_model,
            BaseModel,
        ):
            raise TypeError("arguments_model must be a Pydantic model")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("tool timeout must be between 0 and 300 seconds")
        normalized_sources = frozenset(self.allowed_sources)
        if not normalized_sources:
            raise ValueError("tool allowed sources must not be empty")
        if any(
            not isinstance(source, ToolSource)
            for source in normalized_sources
        ):
            raise TypeError("tool allowed sources must contain ToolSource values")
        normalized_channels = frozenset(self.allowed_channels)
        if not normalized_channels:
            raise ValueError("tool allowed channels must not be empty")
        if any(
            not isinstance(channel, MessageSource)
            for channel in normalized_channels
        ):
            raise TypeError(
                "tool allowed channels must contain MessageSource values"
            )
        normalized_fields = frozenset(
            field_name.casefold()
            for field_name in self.sensitive_fields
            if field_name.strip()
        )
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "impact", self.impact.strip())
        object.__setattr__(self, "sensitive_fields", normalized_fields)
        object.__setattr__(self, "allowed_sources", normalized_sources)
        object.__setattr__(self, "allowed_channels", normalized_channels)


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError("tool name is already registered")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def list(self) -> Sequence[ToolDefinition]:
        return tuple(
            self._definitions[name]
            for name in sorted(self._definitions)
        )
