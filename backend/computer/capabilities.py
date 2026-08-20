"""Capability registration and channel visibility policy."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from computer.models import ComputerPlatform, ModelAccess, ProviderResult
from domain.messages import MessageSource
from domain.tools import ToolRisk


RuntimeProfile = Literal["desktop", "cloud"]
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


class StateProvider(Protocol):
    async def collect(self) -> ProviderResult: ...


class ActionProvider(Protocol):
    async def execute(self, arguments: BaseModel) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    name: str
    title: str
    platforms: frozenset[ComputerPlatform]
    runtime_profiles: frozenset[RuntimeProfile]
    risk: ToolRisk
    model_access: ModelAccess
    allowed_channels: frozenset[MessageSource]
    arguments_model: type[BaseModel]
    provider: StateProvider | ActionProvider

    def __post_init__(self) -> None:
        if not _CAPABILITY_PATTERN.fullmatch(self.name) or len(self.name) > 100:
            raise ValueError("computer capability name is invalid")
        if not self.title.strip():
            raise ValueError("computer capability title must not be blank")
        if not self.platforms:
            raise ValueError("computer capability platforms must not be empty")
        if not self.runtime_profiles:
            raise ValueError("computer capability profiles must not be empty")
        if not self.allowed_channels:
            raise ValueError("computer capability channels must not be empty")
        if not isinstance(self.arguments_model, type) or not issubclass(
            self.arguments_model,
            BaseModel,
        ):
            raise TypeError("computer capability arguments must be a model")
        if not callable(getattr(self.provider, "collect", None)) and not callable(
            getattr(self.provider, "execute", None)
        ):
            raise TypeError("computer capability provider is invalid")
        object.__setattr__(self, "title", self.title.strip())


class CapabilityRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, CapabilityDefinition] = {}

    def register(self, definition: CapabilityDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError("computer capability is already registered")
        self._definitions[definition.name] = definition

    def list(self) -> Sequence[CapabilityDefinition]:
        return tuple(
            self._definitions[name]
            for name in sorted(self._definitions)
        )


class ChannelPolicy:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def list_for(
        self,
        *,
        platform: ComputerPlatform,
        runtime_profile: RuntimeProfile,
        channel: MessageSource,
    ) -> Sequence[CapabilityDefinition]:
        return tuple(
            definition
            for definition in self.registry.list()
            if platform in definition.platforms
            and runtime_profile in definition.runtime_profiles
            and channel in definition.allowed_channels
            and definition.model_access is not ModelAccess.HIDDEN
        )
