"""Platform-aware paths used by the settings persistence layer."""

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path


@dataclass(frozen=True, slots=True)
class SettingsPaths:
    root: Path
    settings_file: Path
    journal_file: Path

    @classmethod
    def default(cls) -> "SettingsPaths":
        return cls.from_root(
            user_config_path("Virtual Anime Assistant", appauthor=False)
        )

    @classmethod
    def from_root(cls, root: Path) -> "SettingsPaths":
        return cls(
            root=root,
            settings_file=root / "settings.json",
            journal_file=root / "settings.save-journal.json",
        )
