from typing import Any

from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    MessageSource,
    ScenarioContent,
    SenderIdentity,
)
from domain.responses import AssistantResponse


LOCAL_USER = SenderIdentity(id="local-user", display_name="本机用户")
SCENARIO_SENDER = SenderIdentity(id="scenario-engine", display_name="场景引擎")
_MESSAGE_ID_ERROR = "messageId must be between 1 and 200 characters"


def optional_client_message_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    message_id = value.strip()
    if not message_id:
        return None
    if len(message_id) > 200:
        raise ValueError(_MESSAGE_ID_ERROR)
    return message_id


def desktop_chat_to_message(
    sender_id: str,
    content: str,
    message_id: str | None = None,
) -> IncomingMessage:
    sender = SenderIdentity(id=sender_id)
    message_data: dict[str, Any] = {
        "conversation_id": f"desktop:{sender_id}",
        "source": MessageSource.DESKTOP,
        "sender": sender,
        "content": ChatContent(text=content),
    }
    validated_message_id = optional_client_message_id(message_id)
    if validated_message_id is not None:
        message_data["message_id"] = validated_message_id
    return IncomingMessage(
        **message_data,
    )


def client_payload_to_message(payload: dict[str, Any]) -> IncomingMessage:
    message_type = payload.get("type")
    sender_id = str(payload.get("senderId") or LOCAL_USER.id)
    conversation_id = str(payload.get("conversationId") or f"desktop:{sender_id}")

    if message_type == "chat":
        message_id = payload.get("messageId")
        message_data: dict[str, Any] = {
            "conversation_id": conversation_id,
            "source": MessageSource.DESKTOP,
            "sender": SenderIdentity(id=sender_id),
            "content": ChatContent(text=str(payload.get("content") or "")),
        }
        validated_message_id = optional_client_message_id(message_id)
        if validated_message_id is not None:
            message_data["message_id"] = validated_message_id
        return IncomingMessage(
            **message_data,
        )
    if message_type == "interaction":
        return IncomingMessage(
            conversation_id=conversation_id,
            source=MessageSource.DESKTOP,
            sender=SenderIdentity(id=sender_id),
            content=InteractionContent(
                action=str(payload.get("action") or "click"),
                x=payload.get("x"),
                y=payload.get("y"),
            ),
        )
    raise ValueError(f"unsupported message type: {message_type!r}")


def scenario_result_to_message(result: dict[str, Any]) -> IncomingMessage:
    scenario_id = str(result["scenarioId"])
    return IncomingMessage(
        conversation_id=f"scenario:{scenario_id}",
        source=MessageSource.SCENARIO,
        sender=SCENARIO_SENDER,
        content=ScenarioContent(
            scenario_id=scenario_id,
            text=str(result["text"]),
            expression=result.get("expression"),
            motion=result.get("motion"),
        ),
    )


def response_to_desktop_payload(response: AssistantResponse) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": response.kind.value,
        "correlationId": response.correlation_id,
    }
    if response.text is not None:
        payload["text"] = response.text
    if response.avatar is not None:
        if response.avatar.expression is not None:
            payload["expression"] = response.avatar.expression
        if response.avatar.motion is not None:
            payload["motion"] = response.avatar.motion
    if response.audio_url is not None:
        payload["audioUrl"] = response.audio_url
    return payload
