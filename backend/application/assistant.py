from time import perf_counter
from typing import Protocol, runtime_checkable

from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.sessions import SessionRegistry, SessionState
from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    ScenarioContent,
)
from domain.responses import AssistantResponse, AvatarCue, ResponseKind
from llm.errors import (
    ModelAuthenticationError,
    ModelGatewayError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from llm.gateway import LanguageModelGateway
from llm.models import ModelRequest
from memory.commands import MemoryCommandType, parse_memory_command
from memory.models import MemoryItem, MessageStatus, ModelCallRecord, StoredMessage


@runtime_checkable
class AssistantStore(Protocol):
    async def claim_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool: ...

    async def claim_message(self, message: StoredMessage) -> bool: ...

    async def save_message(self, message: StoredMessage) -> None: ...

    async def find_message(
        self,
        message_id: str,
    ) -> StoredMessage | None: ...

    async def find_assistant_by_correlation(
        self,
        correlation_id: str,
    ) -> StoredMessage | None: ...

    async def recent_messages(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]: ...

    async def save_memory(self, item: MemoryItem) -> MemoryItem: ...

    async def list_memories(
        self,
        source: str,
        owner_id: str,
    ) -> list[MemoryItem]: ...

    async def delete_memory_by_content(
        self,
        source: str,
        owner_id: str,
        normalized_content: str,
    ) -> bool: ...

    async def record_model_call(self, record: ModelCallRecord) -> None: ...

    async def save_model_result(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None: ...


_DUPLICATE_MESSAGE_ERROR = "无法安全重放该消息，请使用新的消息编号重试。"
_MODEL_ERROR_MESSAGES = (
    (ModelAuthenticationError, "模型服务认证失败，请检查配置。"),
    (ModelRateLimitError, "请求过于频繁，请稍后再试。"),
    (ModelTimeoutError, "模型响应超时，请稍后再试。"),
    (ModelProtocolError, "模型服务返回了无法处理的响应。"),
)
_GENERIC_MODEL_ERROR = "模型服务暂时不可用，请稍后再试。"


class AssistantApplication:
    def __init__(
        self,
        *,
        tts,
        llm: LanguageModelGateway,
        store: AssistantStore,
        context_builder: ConversationContextBuilder,
        publisher: ResponsePublisher | None = None,
        sessions: SessionRegistry | None = None,
    ):
        self.tts = tts
        self.llm = llm
        self.store = store
        self.context_builder = context_builder
        self.publisher = publisher or ResponsePublisher()
        self.sessions = sessions or SessionRegistry()

    async def process(self, message: IncomingMessage) -> AssistantResponse:
        return await self.sessions.run(message, self._handle_in_session)

    async def handle(self, message: IncomingMessage) -> AssistantResponse:
        response = await self.process(message)
        await self.publisher.publish(response)
        return response

    async def has_seen_message(self, message_id: str) -> bool:
        return await self.store.find_message(message_id) is not None

    async def _handle_in_session(
        self,
        message: IncomingMessage,
        _: SessionState,
    ) -> AssistantResponse:
        content = message.content
        if isinstance(content, ChatContent):
            return await self._handle_chat(message, content)
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

    async def _handle_chat(
        self,
        message: IncomingMessage,
        content: ChatContent,
    ) -> AssistantResponse:
        source = message.source.value
        owner_id = message.sender.id
        claimed_conversation = await self.store.claim_conversation(
            message.conversation_id,
            source,
            owner_id,
        )
        if not claimed_conversation:
            return self._conflict_response(message)

        normalized_content = content.text.strip()
        user_message = StoredMessage(
            id=message.message_id,
            conversation_id=message.conversation_id,
            role="user",
            content=normalized_content,
            created_at=message.timestamp,
        )
        if not await self.store.claim_message(user_message):
            stored_user = await self.store.find_message(message.message_id)
            if (
                stored_user is None
                or stored_user.role != "user"
                or stored_user.conversation_id != message.conversation_id
                or stored_user.content != normalized_content
            ):
                return self._conflict_response(message)
            return await self._replay_response(message)

        try:
            memory_command = parse_memory_command(content.text)
        except ValueError:
            return await self._save_local_response(
                message,
                text="记忆内容不能为空。",
                kind=ResponseKind.ERROR,
            )

        if memory_command is not None:
            if memory_command.type is MemoryCommandType.REMEMBER:
                await self.store.save_memory(
                    MemoryItem(
                        source=source,
                        owner_id=owner_id,
                        content=memory_command.content,
                        normalized_content=memory_command.normalized_content,
                        source_message_id=user_message.id,
                    )
                )
                return await self._save_local_response(
                    message,
                    text="已经记住了。",
                    kind=ResponseKind.SPEAK,
                )

            deleted = await self.store.delete_memory_by_content(
                source,
                owner_id,
                memory_command.normalized_content,
            )
            return await self._save_local_response(
                message,
                text=(
                    "已经忘记了。"
                    if deleted
                    else "没有找到完全匹配的记忆。"
                ),
                kind=ResponseKind.SPEAK,
            )

        history = await self.store.recent_messages(
            message.conversation_id,
            self.context_builder.max_messages,
        )
        memories = await self.store.list_memories(source, owner_id)
        request = ModelRequest(
            correlation_id=message.message_id,
            messages=self.context_builder.build(history, memories),
        )
        started_at = perf_counter()
        try:
            reply = await self.llm.complete(request)
        except ModelGatewayError as exc:
            latency_ms = self._elapsed_ms(started_at)
            await self.store.record_model_call(
                ModelCallRecord(
                    message_id=user_message.id,
                    model=self.llm.model_name,
                    status=exc.code,
                    latency_ms=latency_ms,
                )
            )
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.ERROR,
                text=self._safe_model_error(exc),
            )

        response = AssistantResponse(
            correlation_id=message.message_id,
            conversation_id=message.conversation_id,
            kind=ResponseKind.SPEAK,
            text=reply.text,
        )
        assistant_message = StoredMessage(
            id=response.response_id,
            conversation_id=message.conversation_id,
            correlation_id=user_message.id,
            role="assistant",
            content=reply.text,
            model=reply.model,
        )
        record = ModelCallRecord(
            message_id=user_message.id,
            model=reply.model,
            status="succeeded",
            latency_ms=self._elapsed_ms(started_at),
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            provider_request_id=reply.provider_request_id,
        )
        await self.store.save_model_result(record, assistant_message)
        return response

    async def _replay_response(
        self,
        message: IncomingMessage,
    ) -> AssistantResponse:
        stored = await self.store.find_assistant_by_correlation(
            message.message_id
        )
        if (
            stored is None
            or stored.conversation_id != message.conversation_id
        ):
            return self._conflict_response(message)

        kind = (
            ResponseKind.ERROR
            if stored.status is MessageStatus.FAILED
            else ResponseKind.SPEAK
        )
        return AssistantResponse(
            response_id=stored.id,
            correlation_id=message.message_id,
            conversation_id=message.conversation_id,
            kind=kind,
            text=stored.content,
        )

    @staticmethod
    def _conflict_response(
        message: IncomingMessage,
    ) -> AssistantResponse:
        return AssistantResponse(
            correlation_id=message.message_id,
            conversation_id=message.conversation_id,
            kind=ResponseKind.ERROR,
            text=_DUPLICATE_MESSAGE_ERROR,
        )

    async def _save_local_response(
        self,
        message: IncomingMessage,
        *,
        text: str,
        kind: ResponseKind,
    ) -> AssistantResponse:
        response = AssistantResponse(
            correlation_id=message.message_id,
            conversation_id=message.conversation_id,
            kind=kind,
            text=text,
        )
        await self.store.save_message(
            StoredMessage(
                id=response.response_id,
                conversation_id=message.conversation_id,
                correlation_id=message.message_id,
                role="assistant",
                content=text,
                model="local-memory",
                status=(
                    MessageStatus.FAILED
                    if kind is ResponseKind.ERROR
                    else MessageStatus.COMPLETED
                ),
            )
        )
        return response

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, int((perf_counter() - started_at) * 1000))

    @staticmethod
    def _safe_model_error(error: ModelGatewayError) -> str:
        for error_type, message in _MODEL_ERROR_MESSAGES:
            if isinstance(error, error_type):
                return message
        return _GENERIC_MODEL_ERROR
