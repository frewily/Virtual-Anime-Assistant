"""Stable OneBot channel models and error codes."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal


QQ_DISABLED = "qq_disabled"
QQ_MISCONFIGURED = "qq_misconfigured"
ONEBOT_AUTHENTICATION_FAILED = "onebot_authentication_failed"
ONEBOT_DUPLICATE_CONNECTION = "onebot_duplicate_connection"
ONEBOT_INVALID_EVENT = "onebot_invalid_event"
ONEBOT_RATE_LIMITED = "onebot_rate_limited"
ONEBOT_DISCONNECTED = "onebot_disconnected"
ONEBOT_ACTION_TIMEOUT = "onebot_action_timeout"
ONEBOT_ACTION_FAILED = "onebot_action_failed"


class QQState(str, Enum):
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


class OneBotChannelError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OneBotAction:
    action: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class ParsedOneBotMessage:
    self_id: int
    user_id: int
    message_id: int
    message_type: Literal["private", "group"]
    group_id: int | None
    text: str
    mentioned_bot: bool

    @property
    def stable_message_id(self) -> str:
        return f"qq:{self.self_id}:{self.message_id}"

    @property
    def conversation_id(self) -> str:
        if self.message_type == "private":
            return f"qq:private:{self.user_id}"
        return f"qq:group:{self.group_id}:user:{self.user_id}"
