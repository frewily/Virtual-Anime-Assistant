import sys
import unittest
from datetime import timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.messages import ChatContent, IncomingMessage, MessageSource, SenderIdentity
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class DomainModelTests(unittest.TestCase):
    def test_message_has_unique_id_and_aware_timestamp(self):
        sender = SenderIdentity(id="local-user", display_name="本机用户")
        first = IncomingMessage(
            conversation_id="desktop:local-user",
            source=MessageSource.DESKTOP,
            sender=sender,
            content=ChatContent(text="你好"),
        )
        second = IncomingMessage(
            conversation_id="desktop:local-user",
            source=MessageSource.DESKTOP,
            sender=sender,
            content=ChatContent(text="再见"),
        )

        self.assertNotEqual(first.message_id, second.message_id)
        self.assertIs(first.timestamp.tzinfo, timezone.utc)

    def test_chat_content_rejects_blank_text(self):
        with self.assertRaises(ValidationError):
            ChatContent(text="   ")

    def test_avatar_intensity_is_limited_to_unit_interval(self):
        with self.assertRaises(ValidationError):
            AvatarCue(emotion="happy", motion="wave", intensity=1.1)

    def test_speak_response_requires_text(self):
        with self.assertRaises(ValidationError):
            AssistantResponse(
                correlation_id="message-1",
                conversation_id="desktop:local-user",
                kind=ResponseKind.SPEAK,
                avatar=AvatarCue(emotion="happy", motion="wave"),
            )
