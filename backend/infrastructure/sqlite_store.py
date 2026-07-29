import asyncio
import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from domain.tools import (
    ConfirmationState,
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolDecision,
    ToolDecisionClaim,
    ToolRequestRecord,
    ToolRequestState,
    ToolRisk,
    ToolSource,
)
from memory.models import MemoryItem, MessageStatus, ModelCallRecord, StoredMessage


_MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        title TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL
            REFERENCES conversations(id) ON DELETE CASCADE,
        correlation_id TEXT,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
        content TEXT NOT NULL,
        model TEXT,
        status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_messages_conversation_created
    ON messages(conversation_id, created_at)
    """,
    """
    CREATE INDEX idx_messages_correlation
    ON messages(correlation_id)
    """,
    """
    CREATE TABLE memory_items (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        content TEXT NOT NULL,
        normalized_content TEXT NOT NULL,
        source_message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source, owner_id, normalized_content)
    )
    """,
    """
    CREATE INDEX idx_memories_owner
    ON memory_items(source, owner_id, updated_at)
    """,
    """
    CREATE TABLE model_calls (
        id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL
            REFERENCES messages(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
        prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
        completion_tokens INTEGER
            CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
        provider_request_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
)

_MIGRATION_2_STATEMENTS = (
    """
    CREATE TABLE tool_requests (
        id TEXT PRIMARY KEY,
        correlation_id TEXT NOT NULL,
        source TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        title TEXT NOT NULL,
        risk TEXT NOT NULL CHECK (risk IN ('low', 'high')),
        state TEXT NOT NULL CHECK (
            state IN (
                'created',
                'pending_confirmation',
                'running',
                'succeeded',
                'failed',
                'rejected',
                'expired',
                'cancelled'
            )
        ),
        arguments_json TEXT NOT NULL,
        impact TEXT NOT NULL,
        cancellable INTEGER NOT NULL CHECK (cancellable IN (0, 1)),
        timeout_seconds REAL NOT NULL CHECK (timeout_seconds > 0),
        result_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_tool_requests_correlation
    ON tool_requests(correlation_id)
    """,
    """
    CREATE INDEX idx_tool_requests_state_created
    ON tool_requests(state, created_at)
    """,
    """
    CREATE TABLE tool_confirmations (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE
            REFERENCES tool_requests(id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')
        ),
        requested_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT
    )
    """,
    """
    CREATE INDEX idx_tool_confirmations_state_expires
    ON tool_confirmations(state, expires_at)
    """,
    """
    CREATE TABLE tool_audit_events (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL
            REFERENCES tool_requests(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_tool_audit_request_created
    ON tool_audit_events(request_id, created_at)
    """,
)

class SqliteStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        try:
            self._configure_connection()
            self._apply_migrations()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            return int(row[0])

    @property
    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            row = self._connection.execute("PRAGMA foreign_keys").fetchone()
            return bool(row[0])

    def table_names(self) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return {str(row[0]) for row in rows}

    async def upsert_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
        title: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._upsert_conversation_sync,
            conversation_id,
            source,
            owner_id,
            title,
        )

    async def claim_conversation(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._claim_conversation_sync,
            conversation_id,
            source,
            owner_id,
        )

    async def has_message(self, message_id: str) -> bool:
        return await asyncio.to_thread(self._has_message_sync, message_id)

    async def claim_message(self, message: StoredMessage) -> bool:
        return await asyncio.to_thread(self._claim_message_sync, message)

    async def save_message(self, message: StoredMessage) -> None:
        await asyncio.to_thread(self._save_message_sync, message)

    async def find_message(
        self,
        message_id: str,
    ) -> StoredMessage | None:
        return await asyncio.to_thread(self._find_message_sync, message_id)

    async def find_assistant_by_correlation(
        self,
        correlation_id: str,
    ) -> StoredMessage | None:
        return await asyncio.to_thread(
            self._find_assistant_by_correlation_sync,
            correlation_id,
        )

    async def recent_messages(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]:
        return await asyncio.to_thread(
            self._recent_messages_sync,
            conversation_id,
            limit,
        )

    async def list_messages(self, conversation_id: str) -> list[StoredMessage]:
        return await asyncio.to_thread(self._list_messages_sync, conversation_id)

    async def delete_conversation(self, conversation_id: str) -> bool:
        return await asyncio.to_thread(
            self._delete_conversation_sync,
            conversation_id,
        )

    async def save_memory(self, item: MemoryItem) -> MemoryItem:
        return await asyncio.to_thread(self._save_memory_sync, item)

    async def list_memories(
        self,
        source: str,
        owner_id: str,
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(
            self._list_memories_sync,
            source,
            owner_id,
        )

    async def delete_memory_by_content(
        self,
        source: str,
        owner_id: str,
        normalized_content: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._delete_memory_by_content_sync,
            source,
            owner_id,
            normalized_content,
        )

    async def delete_memory_by_id(
        self,
        memory_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._delete_memory_by_id_sync,
            memory_id,
            source,
            owner_id,
        )

    async def record_model_call(self, record: ModelCallRecord) -> None:
        await asyncio.to_thread(self._record_model_call_sync, record)

    async def save_model_result(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None:
        await self.save_model_results((record,), assistant_message)

    async def save_model_results(
        self,
        records: Sequence[ModelCallRecord],
        assistant_message: StoredMessage,
    ) -> None:
        await asyncio.to_thread(
            self._save_model_results_sync,
            tuple(records),
            assistant_message,
        )

    async def create_request(
        self,
        record: ToolRequestRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        await asyncio.to_thread(
            self._create_tool_request_sync,
            record,
            events,
        )

    async def create_confirmation(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        await asyncio.to_thread(
            self._create_tool_confirmation_sync,
            request,
            confirmation,
            events,
        )

    async def claim_decision(
        self,
        confirmation_id: str,
        decision: ToolDecision,
        now: datetime,
    ) -> ToolDecisionClaim | None:
        return await asyncio.to_thread(
            self._claim_tool_decision_sync,
            confirmation_id,
            decision,
            now,
        )

    async def transition_request(
        self,
        request_id: str,
        expected: set[ToolRequestState],
        state: ToolRequestState,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        event: ToolAuditEvent,
    ) -> ToolRequestRecord | None:
        return await asyncio.to_thread(
            self._transition_tool_request_sync,
            request_id,
            expected,
            state,
            result,
            error_code,
            event,
        )

    async def cancel_request(
        self,
        request_id: str,
        now: datetime,
    ) -> ToolRequestRecord | None:
        return await asyncio.to_thread(
            self._cancel_tool_request_sync,
            request_id,
            now,
        )

    async def get_request(
        self,
        request_id: str,
    ) -> ToolRequestRecord | None:
        return await asyncio.to_thread(
            self._get_tool_request_sync,
            request_id,
        )

    async def get_confirmation(
        self,
        confirmation_id: str,
    ) -> ToolConfirmationRecord | None:
        return await asyncio.to_thread(
            self._get_tool_confirmation_sync,
            confirmation_id,
        )

    async def get_confirmation_for_request(
        self,
        request_id: str,
    ) -> ToolConfirmationRecord | None:
        return await asyncio.to_thread(
            self._get_tool_confirmation_for_request_sync,
            request_id,
        )

    async def list_pending_confirmations(
        self,
        now: datetime,
    ) -> list[ToolConfirmationRecord]:
        return await asyncio.to_thread(
            self._list_pending_tool_confirmations_sync,
            now,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _upsert_conversation_sync(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
        title: str | None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO conversations (
                    id, source, owner_id, title, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    owner_id = excluded.owner_id,
                    title = COALESCE(excluded.title, conversations.title),
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    source,
                    owner_id,
                    title,
                    timestamp,
                    timestamp,
                ),
            )

    def _claim_conversation_sync(
        self,
        conversation_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO conversations (
                        id,
                        source,
                        owner_id,
                        title,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        conversation_id,
                        source,
                        owner_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT source, owner_id
                    FROM conversations
                    WHERE id = ?
                    """,
                    (conversation_id,),
                ).fetchone()
                matches_scope = (
                    row is not None
                    and str(row["source"]) == source
                    and str(row["owner_id"]) == owner_id
                )
                if matches_scope:
                    self._connection.execute(
                        """
                        UPDATE conversations
                        SET updated_at = ?
                        WHERE id = ? AND source = ? AND owner_id = ?
                        """,
                        (timestamp, conversation_id, source, owner_id),
                    )
                self._connection.commit()
                return matches_scope
            except BaseException:
                self._connection.rollback()
                raise

    def _has_message_sync(self, message_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            return row is not None

    def _claim_message_sync(self, message: StoredMessage) -> bool:
        with self._lock, self._connection:
            cursor = self._insert_message(
                message,
                ignore_duplicate=True,
            )
            return cursor.rowcount > 0

    def _save_message_sync(self, message: StoredMessage) -> None:
        with self._lock, self._connection:
            self._insert_message(message)

    def _find_message_sync(
        self,
        message_id: str,
    ) -> StoredMessage | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            return self._message_from_row(row) if row is not None else None

    def _find_assistant_by_correlation_sync(
        self,
        correlation_id: str,
    ) -> StoredMessage | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT *
                FROM messages
                WHERE correlation_id = ? AND role = 'assistant'
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (correlation_id,),
            ).fetchone()
            return self._message_from_row(row) if row is not None else None

    def _recent_messages_sync(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[StoredMessage]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
            return [self._message_from_row(row) for row in reversed(rows)]

    def _list_messages_sync(self, conversation_id: str) -> list[StoredMessage]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (conversation_id,),
            ).fetchall()
            return [self._message_from_row(row) for row in rows]

    def _delete_conversation_sync(self, conversation_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            return cursor.rowcount > 0

    def _save_memory_sync(self, item: MemoryItem) -> MemoryItem:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO memory_items (
                    id,
                    source,
                    owner_id,
                    content,
                    normalized_content,
                    source_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, owner_id, normalized_content) DO UPDATE SET
                    content = excluded.content,
                    source_message_id = excluded.source_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    item.id,
                    item.source,
                    item.owner_id,
                    item.content,
                    item.normalized_content,
                    item.source_message_id,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )
            row = self._connection.execute(
                """
                SELECT *
                FROM memory_items
                WHERE source = ? AND owner_id = ? AND normalized_content = ?
                """,
                (item.source, item.owner_id, item.normalized_content),
            ).fetchone()
            if row is None:
                raise RuntimeError("saved memory could not be read back")
            return self._memory_from_row(row)

    def _list_memories_sync(
        self,
        source: str,
        owner_id: str,
    ) -> list[MemoryItem]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM memory_items
                WHERE source = ? AND owner_id = ?
                ORDER BY updated_at DESC, rowid DESC
                """,
                (source, owner_id),
            ).fetchall()
            return [self._memory_from_row(row) for row in rows]

    def _delete_memory_by_content_sync(
        self,
        source: str,
        owner_id: str,
        normalized_content: str,
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM memory_items
                WHERE source = ? AND owner_id = ? AND normalized_content = ?
                """,
                (source, owner_id, normalized_content),
            )
            return cursor.rowcount > 0

    def _delete_memory_by_id_sync(
        self,
        memory_id: str,
        source: str,
        owner_id: str,
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM memory_items
                WHERE id = ? AND source = ? AND owner_id = ?
                """,
                (memory_id, source, owner_id),
            )
            return cursor.rowcount > 0

    def _record_model_call_sync(self, record: ModelCallRecord) -> None:
        with self._lock, self._connection:
            self._insert_model_call(record)

    def _save_model_results_sync(
        self,
        records: Sequence[ModelCallRecord],
        assistant_message: StoredMessage,
    ) -> None:
        if not records:
            raise ValueError("at least one model call is required")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_message(assistant_message)
                for record in records:
                    self._insert_model_call(record)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _create_tool_request_sync(
        self,
        record: ToolRequestRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        self._validate_tool_events(record.request_id, events)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_tool_request(record)
                for event in events:
                    self._insert_tool_audit_event(event)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _create_tool_confirmation_sync(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        if confirmation.request_id != request.request_id:
            raise ValueError("confirmation request does not match request")
        self._validate_tool_events(request.request_id, events)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_tool_request(request)
                self._insert_tool_confirmation(confirmation)
                for event in events:
                    self._insert_tool_audit_event(event)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _claim_tool_decision_sync(
        self,
        confirmation_id: str,
        decision: ToolDecision,
        now: datetime,
    ) -> ToolDecisionClaim | None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                confirmation_row = self._connection.execute(
                    "SELECT * FROM tool_confirmations WHERE id = ?",
                    (confirmation_id,),
                ).fetchone()
                if confirmation_row is None:
                    self._connection.commit()
                    return None

                confirmation = self._tool_confirmation_from_row(
                    confirmation_row
                )
                request_row = self._connection.execute(
                    "SELECT * FROM tool_requests WHERE id = ?",
                    (confirmation.request_id,),
                ).fetchone()
                if request_row is None:
                    raise RuntimeError(
                        "tool confirmation has no request"
                    )
                request = self._tool_request_from_row(request_row)
                if confirmation.state is not ConfirmationState.PENDING:
                    self._connection.commit()
                    return ToolDecisionClaim(
                        request=request,
                        confirmation=confirmation,
                        claimed=False,
                    )

                if now >= confirmation.expires_at:
                    confirmation_state = ConfirmationState.EXPIRED
                    request_state = ToolRequestState.EXPIRED
                    event_type = "expired"
                elif decision is ToolDecision.REJECT:
                    confirmation_state = ConfirmationState.REJECTED
                    request_state = ToolRequestState.REJECTED
                    event_type = "rejected"
                else:
                    confirmation_state = ConfirmationState.APPROVED
                    request_state = ToolRequestState.RUNNING
                    event_type = "approved"

                cursor = self._connection.execute(
                    """
                    UPDATE tool_confirmations
                    SET state = ?, decided_at = ?
                    WHERE id = ? AND state = 'pending'
                    """,
                    (
                        confirmation_state.value,
                        now.isoformat(),
                        confirmation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    self._connection.rollback()
                    return self._claim_tool_decision_sync(
                        confirmation_id,
                        decision,
                        now,
                    )
                self._connection.execute(
                    """
                    UPDATE tool_requests
                    SET state = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        request_state.value,
                        now.isoformat(),
                        request.request_id,
                    ),
                )
                self._insert_tool_audit_event(
                    ToolAuditEvent(
                        request_id=request.request_id,
                        event_type=event_type,
                        created_at=now,
                    )
                )
                updated_request = request.model_copy(
                    update={
                        "state": request_state,
                        "updated_at": now,
                    }
                )
                updated_confirmation = confirmation.model_copy(
                    update={
                        "state": confirmation_state,
                        "decided_at": now,
                    }
                )
                self._connection.commit()
                return ToolDecisionClaim(
                    request=updated_request,
                    confirmation=updated_confirmation,
                    claimed=True,
                )
            except BaseException:
                self._connection.rollback()
                raise

    def _transition_tool_request_sync(
        self,
        request_id: str,
        expected: set[ToolRequestState],
        state: ToolRequestState,
        result: dict | None,
        error_code: str | None,
        event: ToolAuditEvent,
    ) -> ToolRequestRecord | None:
        if not expected:
            raise ValueError("expected states cannot be empty")
        if event.request_id != request_id:
            raise ValueError("audit event request does not match request")
        expected_values = sorted(item.value for item in expected)
        placeholders = ", ".join("?" for _ in expected_values)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    f"""
                    UPDATE tool_requests
                    SET state = ?, result_json = ?, error_code = ?,
                        updated_at = ?
                    WHERE id = ? AND state IN ({placeholders})
                    """,
                    (
                        state.value,
                        self._dump_json(result) if result is not None else None,
                        error_code,
                        event.created_at.isoformat(),
                        request_id,
                        *expected_values,
                    ),
                )
                if cursor.rowcount != 1:
                    self._connection.commit()
                    return None
                self._insert_tool_audit_event(event)
                row = self._connection.execute(
                    "SELECT * FROM tool_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                self._connection.commit()
                return self._tool_request_from_row(row)
            except BaseException:
                self._connection.rollback()
                raise

    def _cancel_tool_request_sync(
        self,
        request_id: str,
        now: datetime,
    ) -> ToolRequestRecord | None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM tool_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                request = self._tool_request_from_row(row)
                if request.state not in {
                    ToolRequestState.PENDING_CONFIRMATION,
                    ToolRequestState.RUNNING,
                }:
                    self._connection.commit()
                    return request

                self._connection.execute(
                    """
                    UPDATE tool_requests
                    SET state = 'cancelled', updated_at = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), request_id),
                )
                self._connection.execute(
                    """
                    UPDATE tool_confirmations
                    SET state = 'cancelled', decided_at = ?
                    WHERE request_id = ? AND state = 'pending'
                    """,
                    (now.isoformat(), request_id),
                )
                self._insert_tool_audit_event(
                    ToolAuditEvent(
                        request_id=request_id,
                        event_type="cancelled",
                        created_at=now,
                    )
                )
                self._connection.commit()
                return request.model_copy(
                    update={
                        "state": ToolRequestState.CANCELLED,
                        "updated_at": now,
                    }
                )
            except BaseException:
                self._connection.rollback()
                raise

    def _get_tool_request_sync(
        self,
        request_id: str,
    ) -> ToolRequestRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tool_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            return (
                self._tool_request_from_row(row)
                if row is not None
                else None
            )

    def _get_tool_confirmation_sync(
        self,
        confirmation_id: str,
    ) -> ToolConfirmationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tool_confirmations WHERE id = ?",
                (confirmation_id,),
            ).fetchone()
            return (
                self._tool_confirmation_from_row(row)
                if row is not None
                else None
            )

    def _get_tool_confirmation_for_request_sync(
        self,
        request_id: str,
    ) -> ToolConfirmationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tool_confirmations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return (
                self._tool_confirmation_from_row(row)
                if row is not None
                else None
            )

    def _list_pending_tool_confirmations_sync(
        self,
        now: datetime,
    ) -> list[ToolConfirmationRecord]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                expired_rows = self._connection.execute(
                    """
                    SELECT id, request_id
                    FROM tool_confirmations
                    WHERE state = 'pending' AND expires_at <= ?
                    """,
                    (now.isoformat(),),
                ).fetchall()
                for row in expired_rows:
                    self._connection.execute(
                        """
                        UPDATE tool_confirmations
                        SET state = 'expired', decided_at = ?
                        WHERE id = ? AND state = 'pending'
                        """,
                        (now.isoformat(), str(row["id"])),
                    )
                    self._connection.execute(
                        """
                        UPDATE tool_requests
                        SET state = 'expired', updated_at = ?
                        WHERE id = ? AND state = 'pending_confirmation'
                        """,
                        (now.isoformat(), str(row["request_id"])),
                    )
                    self._insert_tool_audit_event(
                        ToolAuditEvent(
                            request_id=str(row["request_id"]),
                            event_type="expired",
                            created_at=now,
                        )
                    )
                pending_rows = self._connection.execute(
                    """
                    SELECT *
                    FROM tool_confirmations
                    WHERE state = 'pending'
                    ORDER BY requested_at ASC, rowid ASC
                    """
                ).fetchall()
                self._connection.commit()
                return [
                    self._tool_confirmation_from_row(row)
                    for row in pending_rows
                ]
            except BaseException:
                self._connection.rollback()
                raise

    def _insert_tool_request(self, record: ToolRequestRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO tool_requests (
                id, correlation_id, source, tool_name, title, risk, state,
                arguments_json, impact, cancellable, timeout_seconds,
                result_json, error_code, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.request_id,
                record.correlation_id,
                record.source.value,
                record.tool_name,
                record.title,
                record.risk.value,
                record.state.value,
                self._dump_json(record.arguments_summary),
                record.impact,
                int(record.cancellable),
                record.timeout_seconds,
                (
                    self._dump_json(record.result)
                    if record.result is not None
                    else None
                ),
                record.error_code,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )

    def _insert_tool_confirmation(
        self,
        confirmation: ToolConfirmationRecord,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO tool_confirmations (
                id, request_id, state, requested_at, expires_at, decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation.confirmation_id,
                confirmation.request_id,
                confirmation.state.value,
                confirmation.requested_at.isoformat(),
                confirmation.expires_at.isoformat(),
                (
                    confirmation.decided_at.isoformat()
                    if confirmation.decided_at is not None
                    else None
                ),
            ),
        )

    def _insert_tool_audit_event(self, event: ToolAuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO tool_audit_events (
                id, request_id, event_type, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.request_id,
                event.event_type,
                self._dump_json(event.details),
                event.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _validate_tool_events(
        request_id: str,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        if any(event.request_id != request_id for event in events):
            raise ValueError("audit event request does not match request")

    def _insert_message(
        self,
        message: StoredMessage,
        *,
        ignore_duplicate: bool = False,
    ) -> sqlite3.Cursor:
        conflict_clause = (
            "ON CONFLICT(id) DO NOTHING"
            if ignore_duplicate
            else ""
        )
        return self._connection.execute(
            f"""
            INSERT INTO messages (
                id,
                conversation_id,
                correlation_id,
                role,
                content,
                model,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            {conflict_clause}
            """,
            (
                message.id,
                message.conversation_id,
                message.correlation_id,
                message.role,
                message.content,
                message.model,
                message.status.value,
                message.created_at.isoformat(),
            ),
        )

    def _insert_model_call(self, record: ModelCallRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO model_calls (
                id,
                message_id,
                model,
                status,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                provider_request_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.message_id,
                record.model,
                record.status,
                record.latency_ms,
                record.prompt_tokens,
                record.completion_tokens,
                record.provider_request_id,
                record.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            correlation_id=row["correlation_id"],
            role=str(row["role"]),
            content=str(row["content"]),
            model=row["model"],
            status=MessageStatus(str(row["status"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]),
            source=str(row["source"]),
            owner_id=str(row["owner_id"]),
            content=str(row["content"]),
            normalized_content=str(row["normalized_content"]),
            source_message_id=row["source_message_id"],
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @classmethod
    def _tool_request_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ToolRequestRecord:
        return ToolRequestRecord(
            request_id=str(row["id"]),
            correlation_id=str(row["correlation_id"]),
            source=ToolSource(str(row["source"])),
            tool_name=str(row["tool_name"]),
            title=str(row["title"]),
            risk=ToolRisk(str(row["risk"])),
            state=ToolRequestState(str(row["state"])),
            arguments_summary=cls._load_json_object(
                row["arguments_json"]
            ),
            impact=str(row["impact"]),
            cancellable=bool(row["cancellable"]),
            timeout_seconds=float(row["timeout_seconds"]),
            result=(
                cls._load_json_object(row["result_json"])
                if row["result_json"] is not None
                else None
            ),
            error_code=row["error_code"],
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _tool_confirmation_from_row(
        row: sqlite3.Row,
    ) -> ToolConfirmationRecord:
        return ToolConfirmationRecord(
            confirmation_id=str(row["id"]),
            request_id=str(row["request_id"]),
            state=ConfirmationState(str(row["state"])),
            requested_at=datetime.fromisoformat(str(row["requested_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            decided_at=(
                datetime.fromisoformat(str(row["decided_at"]))
                if row["decided_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _dump_json(payload: dict) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _load_json_object(raw: str) -> dict:
        value = json.loads(str(raw))
        if not isinstance(value, dict):
            raise ValueError("stored JSON must be an object")
        return value

    def _close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _configure_connection(self) -> None:
        with self._lock:
            try:
                journal_row = self._connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
            except sqlite3.Error as error:
                raise RuntimeError(
                    "Failed to configure SQLite rollback journal mode (DELETE)"
                ) from error

            journal_mode = (
                str(journal_row[0]).lower() if journal_row is not None else "unknown"
            )
            if journal_mode != "delete":
                raise RuntimeError(
                    "SQLite rollback journal mode is required; "
                    f"expected 'delete', got {journal_mode!r}"
                )

            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=3000")

    def _apply_migrations(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            self._connection.commit()

            applied_versions = {
                int(row[0])
                for row in self._connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            migrations = (
                (1, "initial_schema", _MIGRATION_1_STATEMENTS),
                (2, "tool_permissions", _MIGRATION_2_STATEMENTS),
            )
            for version, name, statements in migrations:
                if version in applied_versions:
                    continue
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, applied_at) VALUES (?, ?, ?)",
                        (
                            version,
                            name,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise
