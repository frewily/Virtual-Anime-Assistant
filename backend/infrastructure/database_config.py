import os
import platform
from dataclasses import dataclass
from pathlib import Path


DATABASE_FILENAME = "assistant.db"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    data_dir: Path
    database_path: Path

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        configured_data_dir = os.getenv("ASSISTANT_DATA_DIR", "").strip()
        if configured_data_dir:
            data_dir = Path(configured_data_dir).expanduser()
        else:
            data_dir = _default_data_dir()

        return cls(
            data_dir=data_dir,
            database_path=data_dir / DATABASE_FILENAME,
        )


def _default_data_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/VirtualAnimeAssistant"

    if system == "Windows":
        appdata = os.getenv("APPDATA", "").strip()
        base_dir = (
            Path(appdata).expanduser()
            if appdata
            else Path.home() / "AppData/Roaming"
        )
        return base_dir / "VirtualAnimeAssistant"

    xdg_data_home = os.getenv("XDG_DATA_HOME", "").strip()
    base_dir = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home
        else Path.home() / ".local/share"
    )
    return base_dir / "virtual-anime-assistant"
