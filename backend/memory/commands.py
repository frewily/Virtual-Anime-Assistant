import re
import unicodedata
from enum import Enum

from pydantic import BaseModel


class MemoryCommandType(str, Enum):
    REMEMBER = "remember"
    FORGET = "forget"


class MemoryCommand(BaseModel):
    type: MemoryCommandType
    content: str
    normalized_content: str


_COMMAND_PATTERN = re.compile(
    r"^(?P<command>记住|忘记)\s*[:：]\s*(?P<content>.*)$",
    re.DOTALL,
)


def normalize_memory_content(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.split()).casefold()


def parse_memory_command(text: str) -> MemoryCommand | None:
    match = _COMMAND_PATTERN.fullmatch(text.strip())
    if match is None:
        return None

    content = match.group("content").strip()
    if not content:
        raise ValueError("记忆内容不能为空")

    command_type = (
        MemoryCommandType.REMEMBER
        if match.group("command") == "记住"
        else MemoryCommandType.FORGET
    )
    return MemoryCommand(
        type=command_type,
        content=content,
        normalized_content=normalize_memory_content(content),
    )
