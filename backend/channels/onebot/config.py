"""Environment-backed OneBot channel configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

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
        try:
            enabled = cast(
                bool,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_ENABLED",
                    values.get("ASSISTANT_QQ_ENABLED"),
                ),
            )
        except ValueError:
            return cls(enabled=True, configuration_error=QQ_MISCONFIGURED)

        try:
            access_token = cast(
                str,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_ACCESS_TOKEN",
                    values.get("ASSISTANT_QQ_ACCESS_TOKEN"),
                ),
            )
            allowed_group_ids = cast(
                frozenset[int],
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_ALLOWED_GROUP_IDS",
                    values.get("ASSISTANT_QQ_ALLOWED_GROUP_IDS"),
                ),
            )
            allowed_user_ids = cast(
                frozenset[int],
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_ALLOWED_USER_IDS",
                    values.get("ASSISTANT_QQ_ALLOWED_USER_IDS"),
                ),
            )
            rate_per_minute = cast(
                int,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_RATE_PER_MINUTE",
                    values.get("ASSISTANT_QQ_RATE_PER_MINUTE"),
                ),
            )
            rate_burst = cast(
                int,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_RATE_BURST",
                    values.get("ASSISTANT_QQ_RATE_BURST"),
                ),
            )
            max_concurrency = cast(
                int,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_MAX_CONCURRENCY",
                    values.get("ASSISTANT_QQ_MAX_CONCURRENCY"),
                ),
            )
            action_timeout_seconds = cast(
                int,
                parse_onebot_environment_field(
                    "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS",
                    values.get("ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS"),
                ),
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


def parse_onebot_environment_field(
    name: str,
    raw_value: str | None,
) -> bool | str | frozenset[int] | int:
    """Parse one environment field with the same rules used by OneBot runtime."""

    if name == "ASSISTANT_QQ_ENABLED":
        return _parse_bool(raw_value, default=False)
    if name == "ASSISTANT_QQ_ACCESS_TOKEN":
        return (raw_value or "").strip()
    if name in {
        "ASSISTANT_QQ_ALLOWED_GROUP_IDS",
        "ASSISTANT_QQ_ALLOWED_USER_IDS",
    }:
        return _parse_ids(raw_value or "")

    integer_fields = {
        "ASSISTANT_QQ_RATE_PER_MINUTE": (10, 1, 120),
        "ASSISTANT_QQ_RATE_BURST": (2, 1, 20),
        "ASSISTANT_QQ_MAX_CONCURRENCY": (4, 1, 32),
        "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS": (10, 1, 60),
    }
    try:
        default, minimum, maximum = integer_fields[name]
    except KeyError:
        raise ValueError("unknown OneBot environment field") from None
    return _bounded_int(
        raw_value,
        default=default,
        minimum=minimum,
        maximum=maximum,
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
