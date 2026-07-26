import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

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

    async def has_message(self, message_id: str) -> bool:
        return await asyncio.to_thread(self._has_message_sync, message_id)

    async def save_message(self, message: StoredMessage) -> None:
        await asyncio.to_thread(self._save_message_sync, message)

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
        await asyncio.to_thread(
            self._save_model_result_sync,
            record,
            assistant_message,
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

    def _has_message_sync(self, message_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            return row is not None

    def _save_message_sync(self, message: StoredMessage) -> None:
        with self._lock, self._connection:
            self._insert_message(message)

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

    def _save_model_result_sync(
        self,
        record: ModelCallRecord,
        assistant_message: StoredMessage,
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_message(assistant_message)
                self._insert_model_call(record)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _insert_message(self, message: StoredMessage) -> None:
        self._connection.execute(
            """
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
            if 1 in applied_versions:
                return

            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in _MIGRATION_1_STATEMENTS:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        1,
                        "initial_schema",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
