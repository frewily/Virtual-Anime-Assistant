from dataclasses import dataclass
from pathlib import Path

import yaml

_workspace = Path(__file__).resolve().parent.parent.parent
_config_dir = _workspace / "config"
_VOICE_CATALOG_ERROR = "invalid voice catalog"


@dataclass(frozen=True)
class VoiceCatalog:
    voices: tuple[dict, ...]
    default_voice: str
    fallback_voice: str


def _load_yaml(filename: str) -> dict:
    filepath = _config_dir / filename
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_scenarios() -> list:
    data = _load_yaml("scenarios.yml")
    return data.get("scenarios", [])


def get_replies() -> dict:
    data = _load_yaml("replies.yml")
    return data.get("replies", {})


def load_voice_catalog(path: str | Path | None = None) -> VoiceCatalog:
    filepath = Path(path) if path is not None else _config_dir / "voices.yml"
    try:
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError(_VOICE_CATALOG_ERROR) from None

    if not isinstance(data, dict):
        raise ValueError(_VOICE_CATALOG_ERROR)
    voices = data.get("voices")
    default = data.get("default")
    if not isinstance(voices, list) or not isinstance(default, dict):
        raise ValueError(_VOICE_CATALOG_ERROR)

    identifiers: set[str] = set()
    for voice in voices:
        if not isinstance(voice, dict):
            raise ValueError(_VOICE_CATALOG_ERROR)
        identifier = voice.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or identifier in identifiers
            or not isinstance(voice.get("name"), str)
            or not isinstance(voice.get("description"), str)
        ):
            raise ValueError(_VOICE_CATALOG_ERROR)
        identifiers.add(identifier)

    default_voice = default.get("voiceId")
    fallback_voice = default.get("fallbackVoice")
    if (
        not isinstance(default_voice, str)
        or default_voice not in identifiers
        or not isinstance(fallback_voice, str)
        or not fallback_voice.strip()
    ):
        raise ValueError(_VOICE_CATALOG_ERROR)

    return VoiceCatalog(
        voices=tuple(voices),
        default_voice=default_voice,
        fallback_voice=fallback_voice,
    )


def get_voices() -> list:
    return list(load_voice_catalog().voices)


def get_default_voice() -> str:
    return load_voice_catalog().default_voice


def get_fallback_voice() -> str:
    return load_voice_catalog().fallback_voice
