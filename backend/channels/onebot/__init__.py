"""OneBot 11 QQ channel adapter."""

from channels.onebot.config import OneBotSettings
from channels.onebot.models import (
    OneBotAction,
    OneBotChannelError,
    ParsedOneBotMessage,
    QQState,
)

__all__ = [
    "OneBotAction",
    "OneBotChannelError",
    "OneBotSettings",
    "ParsedOneBotMessage",
    "QQState",
]
