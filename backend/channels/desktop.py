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


def desktop_chat_to_message(sender_id: str, content: str) -> IncomingMessage:
    sender = SenderIdentity(id=sender_id)
    return IncomingMessage(
        conversation_id=f"desktop:{sender_id}",
        source=MessageSource.DESKTOP,
        sender=sender,
        content=ChatContent(text=content),
    )


def client_payload_to_message(payload: dict[str, Any]) -> IncomingMessage:
    message_type = payload.get("type")
    sender_id = str(payload.get("senderId") or LOCAL_USER.id)
    conversation_id = str(payload.get("conversationId") or f"desktop:{sender_id}")

    if message_type == "chat":
        return IncomingMessage(
            conversation_id=conversation_id,
            source=MessageSource.DESKTOP,
            sender=SenderIdentity(id=sender_id),
            content=ChatContent(text=str(payload.get("content") or "")),
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
