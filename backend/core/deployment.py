"""Runtime profile settings for desktop and cloud deployments."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    profile: Literal["desktop", "cloud"]
    desktop_monitor_enabled: bool
    cloud_monitor_state_file: Path = Path(
        "/data/operations/cloud-monitor-state.json"
    )
    computer_state_report_token: str | None = field(
        default=None,
        repr=False,
    )
    computer_default_device_id: str = "local-mac"

    def __post_init__(self) -> None:
        if type(self.profile) is not str:
            raise TypeError("runtime profile must be a string")
        if self.profile not in {"desktop", "cloud"}:
            raise ValueError("invalid runtime profile")
        if type(self.desktop_monitor_enabled) is not bool:
            raise TypeError("desktop monitor enabled must be a bool")
        if type(self.computer_default_device_id) is not str:
            raise TypeError("computer device id must be a string")
        if re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?",
            self.computer_default_device_id,
        ) is None:
            raise ValueError("invalid computer device id")
        token = self.computer_state_report_token
        if token is None:
            return
        if type(token) is not str:
            raise TypeError("computer state report token must be a string")
        if not token.isascii() or any(
            character < "!" or character > "~"
            for character in token
        ):
            raise ValueError("invalid computer state report token")
        token_size = len(token.encode("ascii"))
        if not 32 <= token_size <= 256:
            raise ValueError("invalid computer state report token")

    @property
    def computer_state_report_enabled(self) -> bool:
        """Cloud receipt is explicitly disabled until a valid token exists."""

        return (
            self.profile == "cloud"
            and self.computer_state_report_token is not None
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DeploymentSettings":
        values = os.environ if environ is None else environ
        profile_value = values.get(
            "ASSISTANT_RUNTIME_PROFILE",
            "desktop",
        )
        if type(profile_value) is not str:
            raise TypeError("runtime profile must be a string")
        profile = profile_value.strip()
        token_value = values.get(
            "ASSISTANT_COMPUTER_STATE_REPORT_TOKEN",
            "",
        )
        if type(token_value) is not str:
            raise TypeError("computer state report token must be a string")
        token = token_value or None
        device_id_value = values.get(
            "ASSISTANT_COMPUTER_DEVICE_ID",
            "local-mac",
        )
        if type(device_id_value) is not str:
            raise TypeError("computer device id must be a string")
        device_id = device_id_value.strip()
        monitor_path_value = values.get(
            "ASSISTANT_CLOUD_MONITOR_STATE_FILE",
            "/data/operations/cloud-monitor-state.json",
        )
        if type(monitor_path_value) is not str:
            raise TypeError("cloud monitor state file must be a string")
        return cls(
            profile=cast(Literal["desktop", "cloud"], profile),
            desktop_monitor_enabled=profile == "desktop",
            cloud_monitor_state_file=Path(
                monitor_path_value.strip()
            ),
            computer_state_report_token=token,
            computer_default_device_id=device_id,
        )
