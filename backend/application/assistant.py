from application.events import ResponsePublisher
from application.sessions import SessionRegistry, SessionState
from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    ScenarioContent,
)
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class AssistantApplication:
    def __init__(
        self,
        *,
        tts,
        publisher: ResponsePublisher | None = None,
        sessions: SessionRegistry | None = None,
    ):
        self.tts = tts
        self.publisher = publisher or ResponsePublisher()
        self.sessions = sessions or SessionRegistry()

    async def handle(self, message: IncomingMessage) -> AssistantResponse:
        response = await self.sessions.run(message, self._handle_in_session)
        await self.publisher.publish(response)
        return response

    async def _handle_in_session(
        self,
        message: IncomingMessage,
        _: SessionState,
    ) -> AssistantResponse:
        content = message.content
        if isinstance(content, ChatContent):
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.SPEAK,
                text="主人说得有道理~",
                avatar=AvatarCue(
                    emotion="happy",
                    expression="happy",
                    motion="wave",
                ),
            )
        if isinstance(content, InteractionContent):
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.ACTION,
                avatar=AvatarCue(
                    emotion="surprised",
                    expression="surprised",
                    motion="tap_body",
                ),
            )
        if isinstance(content, ScenarioContent):
            audio = await self.tts.synthesize(content.text)
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.SPEAK,
                text=content.text,
                avatar=AvatarCue(
                    expression=content.expression,
                    motion=content.motion,
                ),
                audio_url=audio.get("audio_url") if audio else None,
            )
        raise ValueError(f"unsupported content type: {type(content).__name__}")
