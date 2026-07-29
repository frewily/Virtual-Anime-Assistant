from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.tools import ToolRisk
from tools.registry import ToolDefinition, ToolRegistry
from tools.service import ToolExecutionError


class InvalidTimezoneError(ToolExecutionError):
    def __init__(self) -> None:
        super().__init__("invalid_timezone")


class CurrentTimeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str | None = Field(default=None, max_length=100)

    @field_validator("timezone", mode="before")
    @classmethod
    def normalize_timezone(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None


async def current_time(
    arguments: CurrentTimeArguments,
) -> dict[str, str]:
    try:
        zone = (
            ZoneInfo(arguments.timezone)
            if arguments.timezone
            else datetime.now().astimezone().tzinfo
        )
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise InvalidTimezoneError() from exc

    if zone is None:
        raise InvalidTimezoneError()
    now = datetime.now(zone)
    return {
        "iso": now.isoformat(),
        "timezone": str(zone),
    }


def build_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="system.current_time",
            title="读取当前时间",
            arguments_model=CurrentTimeArguments,
            risk=ToolRisk.LOW,
            impact="只读取指定时区的当前时间",
            timeout_seconds=2,
            cancellable=True,
            handler=current_time,
        )
    )
    return registry
