from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import MemoryItem, ModelCallRecord, StoredMessage


@runtime_checkable
class ConversationRepository(Protocol):
    async def claim_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool: ...

    async def upsert_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
        title: str | None = None,
    ) -> None: ...

    async def has_message(self, message_id: str) -> bool: ...

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

    async def list_messages(self, conversation_id: str) -> list[StoredMessage]: ...

    async def delete_conversation(self, conversation_id: str) -> bool: ...


@runtime_checkable
class MemoryRepository(Protocol):
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

    async def delete_memory_by_id(
        self,
        memory_id: str,
        source: str,
        owner_id: str,
    ) -> bool: ...


@runtime_checkable
class ModelCallRepository(Protocol):
    async def record_model_call(self, record: ModelCallRecord) -> None: ...

    async def save_model_result(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None: ...

    async def save_model_results(
        self,
        records: Sequence[ModelCallRecord],
        assistant_message: StoredMessage,
    ) -> None: ...
