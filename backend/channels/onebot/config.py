"""Environment-backed OneBot channel configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from channels.onebot.models import QQ_MISCONFIGURED


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class OneBotSettings:
    enabled: bool = False
    access_token: str = field(default="", repr=False)
    allowed_group_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    rate_per_minute: int = 10
    rate_burst: int = 2
    max_concurrency: int = 4
    action_timeout_seconds: float = 10
    configuration_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.enabled and self.configuration_error is None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "OneBotSettings":
        values = os.environ if environ is None else environ
        raw_enabled = values.get("ASSISTANT_QQ_ENABLED")
        try:
            enabled = _parse_bool(raw_enabled, default=False)
        except ValueError:
            return cls(enabled=True, configuration_error=QQ_MISCONFIGURED)

        try:
            access_token = values.get(
                "ASSISTANT_QQ_ACCESS_TOKEN",
                "",
            ).strip()
            allowed_group_ids = _parse_ids(
                values.get("ASSISTANT_QQ_ALLOWED_GROUP_IDS", "")
            )
            allowed_user_ids = _parse_ids(
                values.get("ASSISTANT_QQ_ALLOWED_USER_IDS", "")
            )
            rate_per_minute = _bounded_int(
                values.get("ASSISTANT_QQ_RATE_PER_MINUTE"),
                default=10,
                minimum=1,
                maximum=120,
            )
            rate_burst = _bounded_int(
                values.get("ASSISTANT_QQ_RATE_BURST"),
                default=2,
                minimum=1,
                maximum=20,
            )
            max_concurrency = _bounded_int(
                values.get("ASSISTANT_QQ_MAX_CONCURRENCY"),
                default=4,
                minimum=1,
                maximum=32,
            )
            action_timeout_seconds = _bounded_int(
                values.get("ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS"),
                default=10,
                minimum=1,
                maximum=60,
            )
            if rate_burst > rate_per_minute:
                raise ValueError("rate burst exceeds per-minute rate")
            if enabled and not 16 <= len(access_token) <= 512:
                raise ValueError("invalid access token")
            if (
                enabled
                and not allowed_group_ids
                and not allowed_user_ids
            ):
                raise ValueError("an allowlist is required")
        except (AttributeError, TypeError, ValueError):
            return cls(
                enabled=enabled,
                configuration_error=QQ_MISCONFIGURED,
            )

        return cls(
            enabled=enabled,
            access_token=access_token,
            allowed_group_ids=allowed_group_ids,
            allowed_user_ids=allowed_user_ids,
            rate_per_minute=rate_per_minute,
            rate_burst=rate_burst,
            max_concurrency=max_concurrency,
            action_timeout_seconds=action_timeout_seconds,
        )


def _parse_bool(raw_value: str | None, *, default: bool) -> bool:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError("invalid boolean")


def _parse_ids(raw_value: str) -> frozenset[int]:
    value = raw_value.strip()
    if not value:
        return frozenset()

    identifiers: set[int] = set()
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            raise ValueError("empty identifier")
        identifier = int(candidate)
        if identifier <= 0:
            raise ValueError("identifier must be positive")
        identifiers.add(identifier)
    return frozenset(identifiers)


def _bounded_int(
    raw_value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if raw_value is None:
        return default
    value = int(raw_value.strip())
    if not minimum <= value <= maximum:
        raise ValueError("integer outside allowed range")
    return value
