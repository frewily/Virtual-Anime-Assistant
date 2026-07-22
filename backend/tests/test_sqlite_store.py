import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timezone
from inspect import signature
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.database_config import DatabaseSettings
from infrastructure.sqlite_store import SqliteStore
from memory.models import MemoryItem, MessageStatus, ModelCallRecord, StoredMessage
from memory.repositories import MemoryRepository


EXPECTED_TABLES = {
    "conversations",
    "memory_items",
    "messages",
    "model_calls",
    "schema_migrations",
}
EXPECTED_INDEXES = {
    "idx_memories_owner",
    "idx_messages_conversation_created",
    "idx_messages_correlation",
}
EXPECTED_COLUMNS = {
    "schema_migrations": [
        ("version", "INTEGER", 0, 1),
        ("name", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ],
    "conversations": [
        ("id", "TEXT", 0, 1),
        ("source", "TEXT", 1, 0),
        ("owner_id", "TEXT", 1, 0),
        ("title", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ],
    "messages": [
        ("id", "TEXT", 0, 1),
        ("conversation_id", "TEXT", 1, 0),
        ("correlation_id", "TEXT", 0, 0),
        ("role", "TEXT", 1, 0),
        ("content", "TEXT", 1, 0),
        ("model", "TEXT", 0, 0),
        ("status", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ],
    "memory_items": [
        ("id", "TEXT", 0, 1),
        ("source", "TEXT", 1, 0),
        ("owner_id", "TEXT", 1, 0),
        ("content", "TEXT", 1, 0),
        ("normalized_content", "TEXT", 1, 0),
        ("source_message_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ],
    "model_calls": [
        ("id", "TEXT", 0, 1),
        ("message_id", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("latency_ms", "INTEGER", 1, 0),
        ("prompt_tokens", "INTEGER", 0, 0),
        ("completion_tokens", "INTEGER", 0, 0),
        ("provider_request_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
    ],
}
EXPECTED_INDEX_COLUMNS = {
    "idx_messages_conversation_created": ["conversation_id", "created_at"],
    "idx_messages_correlation": ["correlation_id"],
    "idx_memories_owner": ["source", "owner_id", "updated_at"],
}


class DatabaseSettingsTests(unittest.TestCase):
    def test_explicit_data_directory_is_stripped_and_expanded(self):
        with patch.dict(
            os.environ,
            {"ASSISTANT_DATA_DIR": "  ~/assistant-data  ", "HOME": "/home/test"},
            clear=True,
        ):
            settings = DatabaseSettings.from_env()

        self.assertEqual(settings.data_dir, Path("/home/test/assistant-data"))
        self.assertEqual(settings.database_path, settings.data_dir / "assistant.db")

    def test_macos_default_uses_application_support(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("infrastructure.database_config.platform.system", return_value="Darwin"),
            patch(
                "infrastructure.database_config.Path.home",
                return_value=Path("/Users/test"),
            ),
        ):
            settings = DatabaseSettings.from_env()

        self.assertEqual(
            settings.data_dir,
            Path("/Users/test/Library/Application Support/VirtualAnimeAssistant"),
        )

    def test_windows_default_prefers_appdata_and_has_home_fallback(self):
        with (
            patch.dict(os.environ, {"APPDATA": " C:/Users/test/Roaming "}, clear=True),
            patch("infrastructure.database_config.platform.system", return_value="Windows"),
        ):
            configured = DatabaseSettings.from_env()

        self.assertEqual(
            configured.data_dir,
            Path("C:/Users/test/Roaming/VirtualAnimeAssistant"),
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("infrastructure.database_config.platform.system", return_value="Windows"),
            patch(
                "infrastructure.database_config.Path.home",
                return_value=Path("C:/Users/test"),
            ),
        ):
            fallback = DatabaseSettings.from_env()

        self.assertEqual(
            fallback.data_dir,
            Path("C:/Users/test/AppData/Roaming/VirtualAnimeAssistant"),
        )

    def test_other_platform_uses_xdg_or_local_share(self):
        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": " /xdg/data "}, clear=True),
            patch("infrastructure.database_config.platform.system", return_value="Linux"),
        ):
            configured = DatabaseSettings.from_env()

        self.assertEqual(
            configured.data_dir,
            Path("/xdg/data/virtual-anime-assistant"),
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("infrastructure.database_config.platform.system", return_value="Linux"),
            patch(
                "infrastructure.database_config.Path.home",
                return_value=Path("/home/test"),
            ),
        ):
            fallback = DatabaseSettings.from_env()

        self.assertEqual(
            fallback.data_dir,
            Path("/home/test/.local/share/virtual-anime-assistant"),
        )


class PersistenceModelTests(unittest.TestCase):
    def test_defaults_are_unique_and_utc_aware(self):
        message = StoredMessage(
            id="message-1",
            conversation_id="conversation-1",
            role="user",
            content="hello",
        )
        first_memory = MemoryItem(
            source="desktop",
            owner_id="local-user",
            content="likes tea",
            normalized_content="likes tea",
        )
        second_memory = MemoryItem(
            source="desktop",
            owner_id="local-user",
            content="likes books",
            normalized_content="likes books",
        )
        call = ModelCallRecord(
            message_id="message-1",
            model="demo",
            status="timeout_error",
            latency_ms=0,
        )

        self.assertEqual(message.status, MessageStatus.COMPLETED)
        self.assertEqual(call.status, "timeout_error")
        self.assertIs(message.created_at.tzinfo, timezone.utc)
        self.assertIs(first_memory.created_at.tzinfo, timezone.utc)
        self.assertIs(first_memory.updated_at.tzinfo, timezone.utc)
        self.assertIs(call.created_at.tzinfo, timezone.utc)
        self.assertNotEqual(first_memory.id, second_memory.id)

    def test_model_call_rejects_negative_metrics(self):
        invalid_fields = ("latency_ms", "prompt_tokens", "completion_tokens")

        for field in invalid_fields:
            with self.subTest(field=field):
                values = {
                    "message_id": "message-1",
                    "model": "demo",
                    "status": MessageStatus.FAILED,
                    "latency_ms": 1,
                    field: -1,
                }
                with self.assertRaises(ValidationError):
                    ModelCallRecord(**values)

    def test_delete_memory_by_id_requires_owner_scope(self):
        parameters = signature(MemoryRepository.delete_memory_by_id).parameters

        self.assertEqual(
            list(parameters),
            ["self", "memory_id", "source", "owner_id"],
        )


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "nested" / "store.db"
        self.store: SqliteStore | None = None

    def tearDown(self):
        if self.store is not None:
            asyncio.run(self.store.close())
        self.temporary_directory.cleanup()

    def open_store(self) -> SqliteStore:
        self.store = SqliteStore(self.database_path)
        return self.store

    def test_new_database_has_version_one_and_exact_schema(self):
        store = self.open_store()

        self.assertTrue(self.database_path.exists())
        self.assertEqual(store.schema_version, 1)
        self.assertEqual(store.table_names(), EXPECTED_TABLES)

        with sqlite3.connect(self.database_path) as connection:
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
                )
            }

        self.assertEqual(indexes, EXPECTED_INDEXES)

    def test_each_table_has_exact_column_contract(self):
        self.open_store()

        with sqlite3.connect(self.database_path) as connection:
            for table_name, expected_columns in EXPECTED_COLUMNS.items():
                with self.subTest(table=table_name):
                    actual_columns = [
                        (row[1], row[2], row[3], row[5])
                        for row in connection.execute(
                            f"PRAGMA table_info({table_name})"
                        ).fetchall()
                    ]
                    self.assertEqual(actual_columns, expected_columns)

    def test_each_index_has_exact_column_order(self):
        self.open_store()

        with sqlite3.connect(self.database_path) as connection:
            for index_name, expected_columns in EXPECTED_INDEX_COLUMNS.items():
                with self.subTest(index=index_name):
                    actual_columns = [
                        row[2]
                        for row in connection.execute(
                            f"PRAGMA index_info({index_name})"
                        ).fetchall()
                    ]
                    self.assertEqual(actual_columns, expected_columns)

    def test_repeated_initialization_does_not_reapply_version_one(self):
        first = self.open_store()
        asyncio.run(first.close())
        self.store = SqliteStore(self.database_path)

        with sqlite3.connect(self.database_path) as connection:
            migration_rows = connection.execute(
                "SELECT version, name FROM schema_migrations"
            ).fetchall()

        self.assertEqual(self.store.schema_version, 1)
        self.assertEqual(migration_rows, [(1, "initial_schema")])

    def test_connection_pragmas_are_configured_without_wal(self):
        store = self.open_store()

        self.assertTrue(store.foreign_keys_enabled)
        self.assertEqual(store._connection.execute("PRAGMA busy_timeout").fetchone()[0], 3000)
        journal_mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertNotEqual(journal_mode.lower(), "wal")

    def test_existing_wal_database_is_switched_to_delete_journal(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            configured_mode = connection.execute(
                "PRAGMA journal_mode=WAL"
            ).fetchone()[0]

        self.assertEqual(configured_mode.lower(), "wal")

        store = self.open_store()
        active_mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(active_mode.lower(), "delete")
        self.assertEqual(store.schema_version, 1)

    def test_schema_enforces_checks_uniqueness_and_foreign_keys(self):
        store = self.open_store()
        connection = store._connection
        conversation_values = (
            "conversation-1",
            "desktop",
            "local-user",
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id, source, owner_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            conversation_values,
        )
        connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages "
                "(id, conversation_id, role, content, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "bad-role",
                    "conversation-1",
                    "tool",
                    "hello",
                    "completed",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages "
                "(id, conversation_id, role, content, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "orphan",
                    "missing-conversation",
                    "user",
                    "hello",
                    "completed",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages "
                "(id, conversation_id, role, content, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "bad-status",
                    "conversation-1",
                    "user",
                    "hello",
                    "pending",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        connection.execute(
            "INSERT INTO messages "
            "(id, conversation_id, role, content, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "message-1",
                "conversation-1",
                "assistant",
                "hello",
                "completed",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO model_calls "
                "(id, message_id, model, status, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "call-1",
                    "message-1",
                    "demo",
                    "completed",
                    -1,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        connection.execute(
            "INSERT INTO model_calls "
            "(id, message_id, model, status, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "call-2",
                "message-1",
                "demo",
                "timeout_error",
                0,
                "2026-01-01T00:00:00+00:00",
            ),
        )

        memory_values = (
            "memory-1",
            "desktop",
            "local-user",
            "Likes tea",
            "likes tea",
            "message-1",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO memory_items "
            "(id, source, owner_id, content, normalized_content, "
            "source_message_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            memory_values,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_items "
                "(id, source, owner_id, content, normalized_content, "
                "source_message_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("memory-2", *memory_values[1:]),
            )

        connection.commit()
        connection.execute(
            "DELETE FROM conversations WHERE id = ?", ("conversation-1",)
        )
        remaining_messages = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            ("conversation-1",),
        ).fetchone()[0]
        remaining_model_calls = connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE message_id = ?",
            ("message-1",),
        ).fetchone()[0]
        retained_source_message = connection.execute(
            "SELECT source_message_id FROM memory_items WHERE id = ?",
            ("memory-1",),
        ).fetchone()[0]
        self.assertEqual(remaining_messages, 0)
        self.assertEqual(remaining_model_calls, 0)
        self.assertEqual(retained_source_message, "message-1")

    def test_failed_migration_rolls_back_schema_and_version(self):
        from infrastructure import sqlite_store

        broken_statements = (
            "CREATE TABLE migration_should_rollback (id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        )

        with patch.object(sqlite_store, "_MIGRATION_1_STATEMENTS", broken_statements):
            with self.assertRaises(sqlite3.Error):
                SqliteStore(self.database_path)

        with sqlite3.connect(self.database_path) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
            rolled_back_table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'migration_should_rollback'"
            ).fetchone()

        self.assertEqual(versions, [])
        self.assertIsNone(rolled_back_table)

    def test_close_is_async_and_idempotent(self):
        store = self.open_store()

        asyncio.run(store.close())
        asyncio.run(store.close())
        self.store = None


if __name__ == "__main__":
    unittest.main()
