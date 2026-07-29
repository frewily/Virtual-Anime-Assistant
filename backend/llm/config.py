import os
from dataclasses import dataclass


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _parse_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    value = raw_value.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of: 1, true, yes, on, 0, false, no, off"
    )


def _optional_text(name: str, *, trim_trailing_slashes: bool = False) -> str | None:
    value = os.getenv(name, "").strip()
    if trim_trailing_slashes:
        value = value.rstrip("/")
    return value or None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value.strip())
    except ValueError as error:
        raise ValueError(
            f"{name} must be an integer from {minimum} to {maximum}"
        ) from error

    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} to {maximum}")
    return value


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool
    tool_calling_enabled: bool
    base_url: str | None
    api_key: str | None
    model: str | None
    timeout_seconds: int
    max_context_messages: int
    max_context_chars: int

    @classmethod
    def from_env(cls) -> "LLMSettings":
        enabled = _parse_bool("ASSISTANT_LLM_ENABLED", default=False)
        base_url = _optional_text(
            "ASSISTANT_LLM_BASE_URL",
            trim_trailing_slashes=True,
        )
        api_key = _optional_text("ASSISTANT_LLM_API_KEY")
        model = _optional_text("ASSISTANT_LLM_MODEL")

        if enabled and base_url is None:
            raise ValueError(
                "ASSISTANT_LLM_BASE_URL is required when ASSISTANT_LLM_ENABLED=true"
            )
        if enabled and model is None:
            raise ValueError(
                "ASSISTANT_LLM_MODEL is required when ASSISTANT_LLM_ENABLED=true"
            )

        return cls(
            enabled=enabled,
            tool_calling_enabled=_parse_bool(
                "ASSISTANT_LLM_TOOL_CALLING_ENABLED",
                default=False,
            ),
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=_bounded_int(
                "ASSISTANT_LLM_TIMEOUT_SECONDS",
                default=60,
                minimum=1,
                maximum=300,
            ),
            max_context_messages=_bounded_int(
                "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES",
                default=20,
                minimum=1,
                maximum=100,
            ),
            max_context_chars=_bounded_int(
                "ASSISTANT_LLM_MAX_CONTEXT_CHARS",
                default=12000,
                minimum=4000,
                maximum=100000,
            ),
        )
