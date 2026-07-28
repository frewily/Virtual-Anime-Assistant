import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.desktop import (
    client_payload_to_message,
    desktop_chat_to_message,
    response_to_desktop_payload,
    scenario_result_to_message,
)
from domain.messages import ChatContent, InteractionContent, ScenarioContent
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class DesktopChannelTests(unittest.TestCase):
    def test_http_chat_is_normalized(self):
        message = desktop_chat_to_message("user-1", "你好")

        self.assertEqual(message.conversation_id, "desktop:user-1")
        self.assertIsInstance(message.content, ChatContent)
        self.assertEqual(message.content.text, "你好")

    def test_http_chat_preserves_a_non_empty_client_message_id(self):
        message = desktop_chat_to_message(
            "user-1",
            "你好",
            message_id="client-message-1",
        )

        self.assertEqual(message.message_id, "client-message-1")

    def test_websocket_chat_only_accepts_non_empty_string_message_ids(self):
        accepted = client_payload_to_message(
            {
                "type": "chat",
                "content": "你好",
                "messageId": f"  {'m' * 200}  ",
            }
        )
        empty = client_payload_to_message(
            {"type": "chat", "content": "你好", "messageId": ""}
        )
        non_string = client_payload_to_message(
            {"type": "chat", "content": "你好", "messageId": 123}
        )

        self.assertEqual(accepted.message_id, "m" * 200)
        self.assertNotEqual(empty.message_id, "")
        self.assertNotEqual(non_string.message_id, "123")
        self.assertNotEqual(empty.message_id, non_string.message_id)

    def test_chat_rejects_oversized_message_id_without_echoing_it(self):
        oversized = "private-" + ("x" * 193)

        for adapter in (
            lambda: desktop_chat_to_message(
                "user-1",
                "你好",
                message_id=oversized,
            ),
            lambda: client_payload_to_message(
                {
                    "type": "chat",
                    "content": "你好",
                    "messageId": oversized,
                }
            ),
        ):
            with self.subTest(adapter=adapter):
                with self.assertRaises(ValueError) as raised:
                    adapter()

                self.assertEqual(
                    str(raised.exception),
                    "messageId must be between 1 and 200 characters",
                )
                self.assertNotIn(oversized, str(raised.exception))

    def test_websocket_interaction_is_normalized(self):
        message = client_payload_to_message(
            {
                "type": "interaction",
                "action": "click",
                "x": 10,
                "y": 20,
                "messageId": "must-not-be-used",
            }
        )

        self.assertIsInstance(message.content, InteractionContent)
        self.assertEqual(message.content.action, "click")
        self.assertEqual(message.content.x, 10)
        self.assertNotEqual(message.message_id, "must-not-be-used")

    def test_scenario_result_is_normalized(self):
        message = scenario_result_to_message(
            {
                "scenarioId": "high_cpu",
                "text": "电脑好热啊",
                "expression": "worried",
                "motion": "shake",
            }
        )

        self.assertIsInstance(message.content, ScenarioContent)
        self.assertEqual(message.content.scenario_id, "high_cpu")

    def test_response_is_flattened_for_existing_renderer(self):
        response = AssistantResponse(
            correlation_id="message-1",
            conversation_id="desktop:user-1",
            kind=ResponseKind.SPEAK,
            text="你好",
            avatar=AvatarCue(expression="happy", motion="wave"),
            audio_url="/api/tts/audio/example.wav",
        )

        self.assertEqual(
            response_to_desktop_payload(response),
            {
                "type": "speak",
                "text": "你好",
                "expression": "happy",
                "motion": "wave",
                "audioUrl": "/api/tts/audio/example.wav",
                "correlationId": "message-1",
            },
        )

    def test_unknown_websocket_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported message type"):
            client_payload_to_message({"type": "unknown"})
