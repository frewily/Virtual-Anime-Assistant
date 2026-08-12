"""Runtime profile settings for desktop and cloud deployments."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    profile: Literal["desktop", "cloud"]
    desktop_monitor_enabled: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DeploymentSettings":
        values = os.environ if environ is None else environ
        profile = values.get(
            "ASSISTANT_RUNTIME_PROFILE",
            "desktop",
        ).strip()
        if profile not in {"desktop", "cloud"}:
            raise ValueError("invalid runtime profile")
        return cls(
            profile=cast(Literal["desktop", "cloud"], profile),
            desktop_monitor_enabled=profile == "desktop",
        )
