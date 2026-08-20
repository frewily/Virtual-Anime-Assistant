"""Versioned, privacy-filtered computer state models."""

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel


_CAPABILITY_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$"
_SNAPSHOT_TTL = timedelta(seconds=45)


class ComputerPlatform(str, Enum):
    MACOS = "macos"


class ModelAccess(str, Enum):
    HIDDEN = "hidden"
    READ_ONLY = "read_only"
    PROPOSE_WITH_CONFIRMATION = "propose_with_confirmation"


class _ComputerModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


class ProviderResult(_ComputerModel):
    capability: str = Field(pattern=_CAPABILITY_PATTERN, max_length=100)
    state: dict[str, Any]


class ComputerSnapshot(_ComputerModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    device_id: str = Field(
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
        max_length=64,
    )
    platform: ComputerPlatform
    collected_at: datetime
    expires_at: datetime
    capabilities: frozenset[str]
    state: dict[str, dict[str, Any]]

    @field_validator("collected_at", "expires_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("computer snapshot timestamps must include timezone")
        return value

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: frozenset[str]) -> frozenset[str]:
        import re

        if not values:
            raise ValueError("computer snapshot must include a capability")
        if any(
            len(value) > 100 or re.fullmatch(_CAPABILITY_PATTERN, value) is None
            for value in values
        ):
            raise ValueError("computer snapshot capability is invalid")
        return values

    @model_validator(mode="after")
    def validate_envelope(self) -> "ComputerSnapshot":
        if self.expires_at - self.collected_at != _SNAPSHOT_TTL:
            raise ValueError("computer snapshot expiry must be 45 seconds")
        if frozenset(self.state) != self.capabilities:
            raise ValueError("computer snapshot state must match capabilities")
        return self

    def is_fresh(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("freshness clock must include timezone")
        return now < self.expires_at
