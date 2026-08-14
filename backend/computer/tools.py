"""Channel-scoped model tools backed by privacy-filtered state readers."""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from computer.models import ComputerSnapshot
from domain.messages import MessageSource
from domain.tools import ToolRisk, ToolSource
from tools.registry import ToolDefinition
from tools.service import ToolExecutionError


class ComputerStateReader(Protocol):
    def latest(self) -> ComputerSnapshot | None: ...

    def is_stale(self) -> bool: ...


class EmptyComputerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
        allowed_sources=frozenset({ToolSource.MODEL}),
        allowed_channels=allowed_channels,
    )
