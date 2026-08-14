"""Strict, redacted cloud operations state snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
)
from pydantic.alias_generators import to_camel

from core.deployment import DeploymentSettings


_STALE_AFTER = timedelta(minutes=3)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )


class CloudOperationsState(_StrictModel):
    schema_version: Literal[1]
    checked_at: datetime
    overall_state: Literal["healthy", "degraded", "alerting", "unknown"]
    vaa_state: Literal["ready", "not_ready", "unavailable", "unknown"]
    onebot_state: Literal[
        "connected",
        "disconnected",
        "disabled",
        "misconfigured",
        "unknown",
    ]
    backup_state: Literal["fresh", "stale", "missing", "unknown"]
    latest_backup_at: datetime | None
    consecutive_onebot_failures: int = Field(ge=0, le=1000)
    recoveries_in_window: int = Field(ge=0, le=2)
    last_recovery_at: datetime | None
    alert_code: Literal[
        "vaa_unavailable",
        "configuration_required",
        "backup_stale",
        "recovery_exhausted",
        "deployment_in_progress",
        "state_invalid",
    ] | None

    @field_validator("checked_at", "latest_backup_at", "last_recovery_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("cloud operation timestamps require a timezone")
        return value


class CloudOperationsSnapshot(_StrictModel):
    available: bool
    checked_at: datetime | None = None
    overall_state: Literal["healthy", "degraded", "alerting", "unknown"] | None = None
    vaa_state: Literal["ready", "not_ready", "unavailable", "unknown"] | None = None
    onebot_state: Literal[
        "connected",
        "disconnected",
        "disabled",
        "misconfigured",
        "unknown",
    ] | None = None
    backup_state: Literal["fresh", "stale", "missing", "unknown"] | None = None
    latest_backup_at: datetime | None = None
    consecutive_onebot_failures: int | None = Field(default=None, ge=0, le=1000)
    recoveries_in_window: int | None = Field(default=None, ge=0, le=2)
    last_recovery_at: datetime | None = None
    alert_code: Literal[
        "vaa_unavailable",
        "configuration_required",
        "backup_stale",
        "recovery_exhausted",
        "deployment_in_progress",
        "state_invalid",
    ] | None = None

    @model_serializer
    def serialize_snapshot(self) -> dict[str, object]:
        if not self.available:
            return {"available": False}
        return {
            "available": True,
            "checkedAt": self.checked_at,
            "overallState": self.overall_state,
            "vaaState": self.vaa_state,
            "onebotState": self.onebot_state,
            "backupState": self.backup_state,
            "latestBackupAt": self.latest_backup_at,
            "consecutiveOnebotFailures": self.consecutive_onebot_failures,
            "recoveriesInWindow": self.recoveries_in_window,
            "lastRecoveryAt": self.last_recovery_at,
            "alertCode": self.alert_code,
        }


class CloudOperationsReader:
    def __init__(
        self,
        *,
        profile: Literal["desktop", "cloud"],
        state_file: Path,
        now: Callable[[], datetime] | None = None,
    ):
        self._profile = profile
        self._state_file = state_file
        self._now = now or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_deployment(
        cls,
        settings: DeploymentSettings,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> "CloudOperationsReader":
        return cls(
            profile=settings.profile,
            state_file=settings.cloud_monitor_state_file,
            now=now,
        )

    def snapshot(self) -> CloudOperationsSnapshot:
        if self._profile != "cloud":
            return CloudOperationsSnapshot(available=False)
        try:
            state = CloudOperationsState.model_validate_json(
                self._state_file.read_bytes()
            )
            now = self._now()
            if now.tzinfo is None:
                raise ValueError("current time requires a timezone")
            if now - state.checked_at > _STALE_AFTER:
                return self._invalid_snapshot()
        except (OSError, ValidationError, ValueError):
            return self._invalid_snapshot()
        return CloudOperationsSnapshot(
            available=True,
            checked_at=state.checked_at,
            overall_state=state.overall_state,
            vaa_state=state.vaa_state,
            onebot_state=state.onebot_state,
            backup_state=state.backup_state,
            latest_backup_at=state.latest_backup_at,
            consecutive_onebot_failures=state.consecutive_onebot_failures,
            recoveries_in_window=state.recoveries_in_window,
            last_recovery_at=state.last_recovery_at,
            alert_code=state.alert_code,
        )

    @staticmethod
    def _invalid_snapshot() -> CloudOperationsSnapshot:
        return CloudOperationsSnapshot(
            available=True,
            checked_at=None,
            overall_state="unknown",
            vaa_state="unknown",
            onebot_state="unknown",
            backup_state="unknown",
            latest_backup_at=None,
            consecutive_onebot_failures=0,
            recoveries_in_window=0,
            last_recovery_at=None,
            alert_code="state_invalid",
        )
