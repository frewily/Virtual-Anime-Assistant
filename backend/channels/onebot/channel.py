"""OneBot channel orchestration and unified message conversion."""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from channels.onebot.models import (
    OneBotAction,
    OneBotChannelError,
    ParsedOneBotMessage,
)
from channels.onebot.parser import parse_onebot_event
from channels.onebot.policy import (
    AdmissionOutcome,
    OneBotAdmissionPolicy,
    RecentMessageRegistry,
    SenderRateLimiter,
)
from domain.messages import (
    ChatContent,
    IncomingMessage,
    MessageSource,
    SenderIdentity,
)
from domain.responses import AssistantResponse, ResponseKind


logger = logging.getLogger(__name__)


class ApplicationProcessor(Protocol):
    async def process(
        self,
        message: IncomingMessage,
    ) -> AssistantResponse: ...

    async def has_seen_message(self, message_id: str) -> bool: ...


def to_incoming_message(
    message: ParsedOneBotMessage,
) -> IncomingMessage:
    metadata = {
        "self_id": message.self_id,
        "user_id": message.user_id,
        "message_id": message.message_id,
    }
    if message.group_id is not None:
        metadata["group_id"] = message.group_id
    return IncomingMessage(
        message_id=message.stable_message_id,
        conversation_id=message.conversation_id,
        source=MessageSource.QQ,
        sender=SenderIdentity(id=str(message.user_id)),
        content=ChatContent(text=message.text),
        metadata=metadata,
    )


def split_reply(text: str, limit: int = 4000) -> Sequence[str]:
    if limit <= 0:
        raise ValueError("reply limit must be positive")
    return tuple(
        text[index : index + limit]
        for index in range(0, len(text), limit)
    )


def private_reply_action(
    message: ParsedOneBotMessage,
    text: str,
) -> OneBotAction:
    return OneBotAction(
        action="send_private_msg",
        params={
            "user_id": message.user_id,
            "message": [
                {"type": "text", "data": {"text": text}},
            ],
        },
    )


def group_reply_action(
    message: ParsedOneBotMessage,
    text: str,
    *,
    first_chunk: bool,
) -> OneBotAction:
    segments: list[dict[str, object]] = []
    if first_chunk:
        segments.extend(
            (
                {
                    "type": "reply",
                    "data": {"id": str(message.message_id)},
                },
                {
                    "type": "at",
                    "data": {"qq": str(message.user_id)},
                },
            )
        )
    segments.append({"type": "text", "data": {"text": text}})
    return OneBotAction(
        action="send_group_msg",
        params={
            "group_id": message.group_id,
            "message": segments,
        },
    )


class OneBotChannel:
    def __init__(
        self,
        *,
        application: ApplicationProcessor,
        settings: OneBotSettings,
        connection: OneBotConnectionManager,
        policy: OneBotAdmissionPolicy | None = None,
        recent_messages: RecentMessageRegistry | None = None,
    ) -> None:
        self._application = application
        self._connection = connection
        if policy is None:
            limiter = SenderRateLimiter(
                rate_per_minute=settings.rate_per_minute,
                burst=settings.rate_burst,
            )
            policy = OneBotAdmissionPolicy(settings, limiter)
        self._policy = policy
        self._recent_messages = (
            recent_messages or RecentMessageRegistry()
        )
        self._concurrency = asyncio.Semaphore(
            settings.max_concurrency
        )

    async def handle_event(
        self,
        payload: Mapping[str, object],
        *,
        self_id: int,
    ) -> None:
        parsed = parse_onebot_event(
            payload,
            expected_self_id=self_id,
        )
        if parsed is None:
            return
        if (
            self._policy.authorize(parsed)
            is not AdmissionOutcome.ALLOW
        ):
            return
        if not self._recent_messages.claim(parsed.stable_message_id):
            return

        try:
            already_seen = await self._application.has_seen_message(
                parsed.stable_message_id
            )
        except Exception:
            self._recent_messages.release(parsed.stable_message_id)
            raise
        if already_seen:
            return
        if (
            self._policy.rate_limit(parsed)
            is not AdmissionOutcome.ALLOW
        ):
            return

        incoming = to_incoming_message(parsed)
        async with self._concurrency:
            response = await self._application.process(incoming)
        await self._send_response(parsed, response)

    async def _send_response(
        self,
        message: ParsedOneBotMessage,
        response: AssistantResponse,
    ) -> None:
        if response.kind is ResponseKind.ACTION:
            return
        text = response.text
        if text is None or not text.strip():
            return

        for index, chunk in enumerate(split_reply(text)):
            action = (
                private_reply_action(message, chunk)
                if message.message_type == "private"
                else group_reply_action(
                    message,
                    chunk,
                    first_chunk=index == 0,
                )
            )
            try:
                await self._connection.send_action(action)
            except OneBotChannelError as exc:
                logger.warning("OneBot reply failed: %s", exc.code)
                return
