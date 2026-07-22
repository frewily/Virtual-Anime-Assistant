import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


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

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

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
