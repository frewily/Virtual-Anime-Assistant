"""OneBot 11 QQ channel adapter."""

from channels.onebot.channel import OneBotChannel
from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from channels.onebot.models import (
    OneBotAction,
    OneBotChannelError,
    ParsedOneBotMessage,
    QQState,
)

__all__ = [
    "OneBotAction",
    "OneBotChannel",
    "OneBotChannelError",
    "OneBotConnectionManager",
    "OneBotSettings",
    "ParsedOneBotMessage",
    "QQState",
]
