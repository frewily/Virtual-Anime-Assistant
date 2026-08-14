"""Fail-closed privacy classification for foreground macOS applications."""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class PrivacyLevel(str, Enum):
    SHOW = "show"
    BROWSER = "browser"
    HIDE_TITLE = "hide_title"
    SECRET = "secret"


@dataclass(frozen=True, slots=True)
class AppClassification:
    level: PrivacyLevel


@dataclass(frozen=True, slots=True)
class SanitizedForeground:
    app_name: str
    window_title: str | None
    privacy_level: PrivacyLevel
    fullscreen: bool


_BROWSER_NAMES = frozenset(
    {
        "arc",
        "brave browser",
        "firefox",
        "google chrome",
        "microsoft edge",
        "safari",
    }
)
_BROWSER_BUNDLES = frozenset(
    {
        "com.apple.safari",
        "com.brave.browser",
        "com.google.chrome",
        "com.microsoft.edgemac",
        "company.thebrowser.browser",
        "org.mozilla.firefox",
    }
)
_HIDE_TITLE_NAMES = frozenset(
    {
        "discord",
        "iterm2",
        "mail",
        "microsoft outlook",
        "qq",
        "slack",
        "telegram",
        "terminal",
        "visual studio code",
        "warp",
        "wechat",
        "微信",
    }
)
_SHOW_NAMES = frozenset(
    {
        "music",
        "neteasemusic",
        "qqmusic",
        "spotify",
        "网易云音乐",
        "qq音乐",
    }
)
_SECRET_MARKERS = (
    "1password",
    "authenticator",
    "bitwarden",
    "keychain access",
    "password manager",
    "securities",
    "银行",
    "证券",
    "密码",
    "验证器",
)


def _normalize_identifier(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    without_controls = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cc"
    )
    return " ".join(without_controls.casefold().split())


def _safe_display_text(value: str | None, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    without_controls = "".join(
        " " if character.isspace() else character
        for character in normalized
        if unicodedata.category(character) != "Cc"
    )
    return re.sub(r"\s+", " ", without_controls).strip()[:limit]


def classify_app(
    app_name: str | None,
    bundle_id: str | None = None,
) -> AppClassification:
    normalized_name = _normalize_identifier(app_name)
    normalized_bundle = _normalize_identifier(bundle_id)
    if not normalized_name:
        return AppClassification(PrivacyLevel.SECRET)
    if any(
        marker in normalized_name or marker in normalized_bundle
        for marker in _SECRET_MARKERS
    ):
        return AppClassification(PrivacyLevel.SECRET)
    if (
        normalized_name in _BROWSER_NAMES
        or normalized_bundle in _BROWSER_BUNDLES
    ):
        return AppClassification(PrivacyLevel.BROWSER)
    if normalized_name in _HIDE_TITLE_NAMES:
        return AppClassification(PrivacyLevel.HIDE_TITLE)
    if normalized_name in _SHOW_NAMES:
        return AppClassification(PrivacyLevel.SHOW)
    return AppClassification(PrivacyLevel.HIDE_TITLE)


def sanitize_foreground(
    app_name: str | None,
    window_title: str | None,
    *,
    bundle_id: str | None = None,
    fullscreen: bool = False,
) -> SanitizedForeground:
    classification = classify_app(app_name, bundle_id)
    if classification.level is PrivacyLevel.SECRET:
        return SanitizedForeground(
            app_name="私密应用",
            window_title=None,
            privacy_level=classification.level,
            fullscreen=bool(fullscreen),
        )
    safe_name = _safe_display_text(app_name, limit=100)
    safe_title = (
        _safe_display_text(window_title, limit=128)
        if classification.level is PrivacyLevel.SHOW
        else ""
    )
    return SanitizedForeground(
        app_name=safe_name or "私密应用",
        window_title=safe_title or None,
        privacy_level=classification.level,
        fullscreen=bool(fullscreen),
    )
