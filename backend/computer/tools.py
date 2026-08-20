"""Channel-scoped tools for privacy-filtered state and bounded actions."""

from collections.abc import Awaitable
from enum import Enum
from typing import Annotated, Protocol
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
)

from computer.models import ComputerSnapshot, ModelAccess
from computer.macos import (
    MacOSActionError,
    normalize_public_https_url,
    validate_application_identifier,
)
from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from tools.registry import ToolDefinition
from tools.service import ToolExecutionError


class ComputerStateReader(Protocol):
    def latest(self) -> ComputerSnapshot | None: ...

    def is_stale(self) -> bool: ...


class MacOSActions(Protocol):
    async def open_application(self, application: str) -> dict[str, str]: ...

    async def open_url(self, url: str) -> dict[str, str]: ...

    async def toggle_media(self, player: str) -> dict[str, str]: ...

    async def set_volume(self, volume: int) -> dict[str, str]: ...


class EmptyComputerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ActionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenApplicationArguments(_ActionArguments):
    application: Annotated[StrictStr, Field(min_length=1, max_length=100)]

    @field_validator("application")
    @classmethod
    def validate_application(cls, value: str) -> str:
        return validate_application_identifier(value)


class OpenUrlArguments(_ActionArguments):
    url: Annotated[StrictStr, Field(min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return normalize_public_https_url(value)


class MediaPlayer(str, Enum):
    MUSIC = "Music"
    SPOTIFY = "Spotify"


class ToggleMediaArguments(_ActionArguments):
    player: MediaPlayer


class SetVolumeArguments(_ActionArguments):
    volume: Annotated[StrictInt, Field(ge=0, le=100)]


class ComputerStateUnavailableError(ToolExecutionError):
    def __init__(self) -> None:
        super().__init__("computer_state_unavailable")


def build_current_state_tool(
    reader: ComputerStateReader,
    *,
    allowed_channels: frozenset[MessageSource],
) -> ToolDefinition:
    async def current_state(_: EmptyComputerArguments) -> dict:
        snapshot = reader.latest()
        if snapshot is None:
            raise ComputerStateUnavailableError()
        unavailable = sorted(
            capability
            for capability, state in snapshot.state.items()
            if state.get("status") == "unavailable"
        )
        payload = snapshot.model_dump(mode="json", by_alias=True)
        payload["freshness"] = "stale" if reader.is_stale() else "fresh"
        payload["unavailableCapabilities"] = unavailable
        return payload

    return ToolDefinition(
        name="computer.current_state",
        title="读取电脑当前状态",
        arguments_model=EmptyComputerArguments,
        risk=ToolRisk.LOW,
        impact="只读取内存中的最新脱敏状态，不读取历史记录",
        timeout_seconds=2,
        cancellable=True,
        handler=current_state,
        model_access=ModelAccess.READ_ONLY,
        allowed_sources=frozenset({ToolSource.MODEL}),
        allowed_channels=allowed_channels,
    )


def summarize_open_url(url: str) -> str:
    """Build the safe confirmation text required by the future UI hook."""

    parsed = urlsplit(OpenUrlArguments(url=url).url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    query = "&".join(
        f"{quote(name, safe='')}=[已隐藏]"
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    )
    return urlunsplit(("https", host, parsed.path or "/", query, ""))


def build_macos_action_tools(
    actions: MacOSActions,
) -> tuple[ToolDefinition, ...]:
    async def execute_action(
        action: Awaitable[dict[str, str]],
    ) -> dict[str, str]:
        try:
            return await action
        except MacOSActionError as exc:
            raise ToolExecutionError("macos_action_failed") from exc

    async def open_application(
        arguments: OpenApplicationArguments,
    ) -> dict[str, str]:
        return await execute_action(
            actions.open_application(arguments.application)
        )

    async def open_url(arguments: OpenUrlArguments) -> dict[str, str]:
        return await execute_action(actions.open_url(arguments.url))

    async def toggle_media(
        arguments: ToggleMediaArguments,
    ) -> dict[str, str]:
        return await execute_action(
            actions.toggle_media(arguments.player.value)
        )

    async def set_volume(arguments: SetVolumeArguments) -> dict[str, str]:
        return await execute_action(actions.set_volume(arguments.volume))

    common = {
        "risk": ToolRisk.HIGH,
        "model_access": ModelAccess.PROPOSE_WITH_CONFIRMATION,
        "timeout_seconds": 5,
        "cancellable": True,
        "allowed_sources": frozenset({ToolSource.MODEL}),
        "allowed_channels": frozenset({MessageSource.DESKTOP}),
    }
    return (
        ToolDefinition(
            name="computer.open_application",
            title="打开 macOS 应用",
            arguments_model=OpenApplicationArguments,
            impact="聚焦或启动指定的 macOS 应用",
            handler=open_application,
            **common,
        ),
        ToolDefinition(
            name="computer.open_url",
            title="打开 HTTPS 链接",
            arguments_model=OpenUrlArguments,
            impact="在默认浏览器中打开指定的公开 HTTPS 链接",
            handler=open_url,
            sensitive_fields=frozenset({"url"}),
            confirmation_summary=lambda arguments: {
                "url": summarize_open_url(arguments.url)
            },
            **common,
        ),
        ToolDefinition(
            name="computer.toggle_media",
            title="切换媒体播放状态",
            arguments_model=ToggleMediaArguments,
            impact="切换 Music 或 Spotify 的播放/暂停状态",
            handler=toggle_media,
            **common,
        ),
        ToolDefinition(
            name="computer.set_volume",
            title="设置系统音量",
            arguments_model=SetVolumeArguments,
            impact="把 macOS 输出音量设置为指定百分比",
            handler=set_volume,
            **common,
        ),
    )
