from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

_workspace = Path(__file__).resolve().parent.parent.parent
_bundled_config_dir = os.getenv("ASSISTANT_BUNDLED_CONFIG_DIR", "").strip()
_config_dir = (
    Path(_bundled_config_dir).expanduser()
    if _bundled_config_dir
    else _workspace / "config"
)
_VOICE_CATALOG_ERROR = "invalid voice catalog"


class _CatalogYamlError(yaml.YAMLError):
    pass


class _StrictSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise _CatalogYamlError("invalid mapping")
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError:
                raise _CatalogYamlError("invalid mapping key") from None
            if duplicate:
                raise _CatalogYamlError("duplicate mapping key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(_VOICE_CATALOG_ERROR)
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if value is None or isinstance(value, (bool, float, int, str)):
        return value
    raise ValueError(_VOICE_CATALOG_ERROR)


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class VoiceCatalog:
    voices: tuple[Mapping[str, object], ...]
    default_voice: str
    fallback_voice: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "voices", _deep_freeze(self.voices))

    def copy_voices(self) -> list[dict]:
        return [_deep_thaw(voice) for voice in self.voices]


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
        data = yaml.load(
            filepath.read_text(encoding="utf-8"),
            Loader=_StrictSafeLoader,
        )
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError(_VOICE_CATALOG_ERROR) from None

    try:
        _deep_freeze(data)
    except ValueError:
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
    return load_voice_catalog().copy_voices()


def get_default_voice() -> str:
    return load_voice_catalog().default_voice


def get_fallback_voice() -> str:
    return load_voice_catalog().fallback_voice
