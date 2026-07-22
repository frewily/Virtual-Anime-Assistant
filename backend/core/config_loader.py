import yaml
from pathlib import Path

_workspace = Path(__file__).resolve().parent.parent.parent
_config_dir = _workspace / "config"


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


def get_voices() -> list:
    data = _load_yaml("voices.yml")
    return data.get("voices", [])


def get_default_voice() -> str:
    data = _load_yaml("voices.yml")
    return data.get("default", {}).get("voiceId", "character_001")


def get_fallback_voice() -> str:
    data = _load_yaml("voices.yml")
    return data.get("default", {}).get("fallbackVoice", "zh-CN-XiaoxiaoNeural")
