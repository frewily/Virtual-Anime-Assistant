"""Pure parsing for untrusted OneBot 11 events."""

from collections.abc import Mapping

from channels.onebot.models import ParsedOneBotMessage


def parse_onebot_event(
    payload: Mapping[str, object],
    *,
    expected_self_id: int,
) -> ParsedOneBotMessage | None:
    if payload.get("post_type") != "message":
        return None

    message_type = payload.get("message_type")
    if message_type not in {"private", "group"}:
        return None

    self_id = _positive_int(payload.get("self_id"))
    user_id = _positive_int(payload.get("user_id"))
    message_id = _positive_int(payload.get("message_id"))
    if (
        self_id is None
        or user_id is None
        or message_id is None
        or self_id != expected_self_id
        or user_id == self_id
    ):
        return None

    group_id: int | None = None
    if message_type == "group":
        group_id = _positive_int(payload.get("group_id"))
        if group_id is None:
            return None

    raw_message = payload.get("message")
    if message_type == "private" and isinstance(raw_message, str):
        text = raw_message.strip()
        mentioned_bot = False
    elif isinstance(raw_message, list):
        text, mentioned_bot = _parse_segments(
            raw_message,
            expected_self_id=expected_self_id,
        )
    else:
        return None

    if not text or len(text) > 4000:
        return None

    return ParsedOneBotMessage(
        self_id=self_id,
        user_id=user_id,
        message_id=message_id,
        message_type=message_type,
        group_id=group_id,
        text=text,
        mentioned_bot=mentioned_bot,
    )


def _parse_segments(
    segments: list[object],
    *,
    expected_self_id: int,
) -> tuple[str, bool]:
    text_parts: list[str] = []
    mentioned_bot = False
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_type = segment.get("type")
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        if segment_type == "text":
            text = data.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        elif segment_type == "at":
            target = data.get("qq")
            if (
                isinstance(target, int)
                and not isinstance(target, bool)
                and target == expected_self_id
            ) or (
                isinstance(target, str)
                and target == str(expected_self_id)
            ):
                mentioned_bot = True
    return "".join(text_parts).strip(), mentioned_bot


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
