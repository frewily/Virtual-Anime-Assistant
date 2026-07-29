import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.database_config import DatabaseSettings
from domain.tools import (
    ConfirmationState,
    ToolAuditEvent,
    ToolConfirmationRecord,
    ToolDecision,
    ToolRequestRecord,
    ToolRequestState,
    ToolRisk,
    ToolSource,
)
from infrastructure.sqlite_store import (
    _MIGRATION_1_STATEMENTS,
    SqliteStore,
)
from memory.models import MemoryItem, MessageStatus, ModelCallRecord, StoredMessage
from memory.repositories import (
    ConversationRepository,
    MemoryRepository,
    ModelCallRepository,
)
from tools.repositories import ToolRepository


EXPECTED_TABLES = {
    "conversations",
    "memory_items",
    "messages",
    "model_calls",
    "schema_migrations",
    "tool_audit_events",
    "tool_confirmations",
    "tool_requests",
}
EXPECTED_INDEXES = {
    "idx_memories_owner",
    "idx_messages_conversation_created",
    "idx_messages_correlation",
    "idx_tool_audit_request_created",
    "idx_tool_confirmations_state_expires",
    "idx_tool_requests_correlation",
    "idx_tool_requests_state_created",
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
    "tool_requests": [
        ("id", "TEXT", 0, 1),
        ("correlation_id", "TEXT", 1, 0),
        ("source", "TEXT", 1, 0),
        ("tool_name", "TEXT", 1, 0),
        ("title", "TEXT", 1, 0),
        ("risk", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("arguments_json", "TEXT", 1, 0),
        ("impact", "TEXT", 1, 0),
        ("cancellable", "INTEGER", 1, 0),
        ("timeout_seconds", "REAL", 1, 0),
        ("result_json", "TEXT", 0, 0),
        ("error_code", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ],
    "tool_confirmations": [
        ("id", "TEXT", 0, 1),
        ("request_id", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("requested_at", "TEXT", 1, 0),
        ("expires_at", "TEXT", 1, 0),
        ("decided_at", "TEXT", 0, 0),
    ],
    "tool_audit_events": [
        ("id", "TEXT", 0, 1),
        ("request_id", "TEXT", 1, 0),
        ("event_type", "TEXT", 1, 0),
        ("details_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ],
}
EXPECTED_INDEX_COLUMNS = {
    "idx_messages_conversation_created": ["conversation_id", "created_at"],
    "idx_messages_correlation": ["correlation_id"],
    "idx_memories_owner": ["source", "owner_id", "updated_at"],
    "idx_tool_requests_correlation": ["correlation_id"],
    "idx_tool_requests_state_created": ["state", "created_at"],
    "idx_tool_confirmations_state_expires": ["state", "expires_at"],
    "idx_tool_audit_request_created": ["request_id", "created_at"],
}


def create_version_one_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        for statement in _MIGRATION_1_STATEMENTS:
            connection.execute(statement)
        timestamp = "2026-07-29T00:00:00+00:00"
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'initial_schema', ?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, source, owner_id, title, created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                "conversation-before-upgrade",
                "desktop",
                "local-user",
                timestamp,
                timestamp,
            ),
        )


def high_risk_records(
    *,
    now: datetime | None = None,
    expires_at: datetime | None = None,
):
    created_at = now or datetime(2026, 7, 29, tzinfo=timezone.utc)
    request = ToolRequestRecord(
        request_id="tool-request-1",
        correlation_id="message-1",
        source=ToolSource.DESKTOP,
        tool_name="computer.open_app",
        title="打开应用",
        risk=ToolRisk.HIGH,
        state=ToolRequestState.PENDING_CONFIRMATION,
        arguments_summary={
            "application": "TextEdit",
            "token": "[REDACTED]",
        },
        impact="将在电脑上打开指定应用",
        cancellable=True,
        timeout_seconds=10,
        created_at=created_at,
        updated_at=created_at,
    )
    confirmation = ToolConfirmationRecord(
        confirmation_id="confirmation-1",
        request_id=request.request_id,
        requested_at=created_at,
        expires_at=expires_at or created_at + timedelta(seconds=60),
    )
    events = [
        ToolAuditEvent(
            event_id="audit-1",
            request_id=request.request_id,
            event_type="requested",
            details={"arguments": request.arguments_summary},
            created_at=created_at,
        ),
        ToolAuditEvent(
            event_id="audit-2",
            request_id=request.request_id,
            event_type="confirmation_required",
            created_at=created_at,
        ),
    ]
    return request, confirmation, events


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

    def test_new_database_has_version_two_and_exact_schema(self):
        store = self.open_store()

        self.assertTrue(self.database_path.exists())
        self.assertEqual(store.schema_version, 2)
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

    def test_repeated_initialization_does_not_reapply_migrations(self):
        first = self.open_store()
        asyncio.run(first.close())
        self.store = SqliteStore(self.database_path)

        with sqlite3.connect(self.database_path) as connection:
            migration_rows = connection.execute(
                "SELECT version, name FROM schema_migrations"
            ).fetchall()

        self.assertEqual(self.store.schema_version, 2)
        self.assertEqual(
            migration_rows,
            [(1, "initial_schema"), (2, "tool_permissions")],
        )

    def test_version_one_database_upgrades_without_data_loss(self):
        create_version_one_database(self.database_path)

        store = self.open_store()

        self.assertEqual(store.schema_version, 2)
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT source, owner_id FROM conversations WHERE id = ?",
                ("conversation-before-upgrade",),
            ).fetchone()
        self.assertEqual(row, ("desktop", "local-user"))

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
        self.assertEqual(store.schema_version, 2)

    def test_tool_confirmation_and_redacted_audit_round_trip_atomically(self):
        store = self.open_store()
        request, confirmation, events = high_risk_records()

        asyncio.run(
            store.create_confirmation(request, confirmation, events)
        )

        stored_request = asyncio.run(store.get_request(request.request_id))
        stored_confirmation = asyncio.run(
            store.get_confirmation(confirmation.confirmation_id)
        )
        with sqlite3.connect(self.database_path) as connection:
            details_json = connection.execute(
                "SELECT details_json FROM tool_audit_events "
                "WHERE id = 'audit-1'"
            ).fetchone()[0]

        self.assertEqual(stored_request, request)
        self.assertEqual(stored_confirmation, confirmation)
        self.assertIn('"token": "[REDACTED]"', details_json)
        self.assertNotIn("private-token", details_json)

    def test_concurrent_confirmation_claim_has_one_winner(self):
        store = self.open_store()
        request, confirmation, events = high_risk_records()
        asyncio.run(
            store.create_confirmation(request, confirmation, events)
        )

        async def claim_twice():
            return await asyncio.gather(
                store.claim_decision(
                    confirmation.confirmation_id,
                    ToolDecision.APPROVE,
                    datetime(2026, 7, 29, 0, 0, 1, tzinfo=timezone.utc),
                ),
                store.claim_decision(
                    confirmation.confirmation_id,
                    ToolDecision.APPROVE,
                    datetime(2026, 7, 29, 0, 0, 1, tzinfo=timezone.utc),
                ),
            )

        claims = asyncio.run(claim_twice())
        winners = [claim for claim in claims if claim and claim.claimed]

        self.assertEqual(len(winners), 1)
        self.assertEqual(
            winners[0].request.state,
            ToolRequestState.RUNNING,
        )

    def test_pending_query_expires_confirmation_and_request(self):
        store = self.open_store()
        now = datetime(2026, 7, 29, tzinfo=timezone.utc)
        request, confirmation, events = high_risk_records(
            now=now,
            expires_at=now + timedelta(seconds=1),
        )
        asyncio.run(
            store.create_confirmation(request, confirmation, events)
        )

        pending = asyncio.run(
            store.list_pending_confirmations(now + timedelta(seconds=2))
        )
        expired_request = asyncio.run(
            store.get_request(request.request_id)
        )
        expired_confirmation = asyncio.run(
            store.get_confirmation(confirmation.confirmation_id)
        )

        self.assertEqual(pending, [])
        self.assertEqual(
            expired_request.state,
            ToolRequestState.EXPIRED,
        )
        self.assertEqual(
            expired_confirmation.state,
            ConfirmationState.EXPIRED,
        )

    def test_low_risk_transition_and_pending_cancel_are_persisted(self):
        store = self.open_store()
        request, confirmation, events = high_risk_records()
        low_risk = request.model_copy(
            update={
                "request_id": "low-risk-request",
                "tool_name": "system.current_time",
                "title": "读取当前时间",
                "risk": ToolRisk.LOW,
                "state": ToolRequestState.RUNNING,
                "impact": "只读取系统时间",
            }
        )
        low_events = [
            event.model_copy(
                update={
                    "event_id": f"low-{event.event_id}",
                    "request_id": low_risk.request_id,
                }
            )
            for event in events
        ]
        asyncio.run(store.create_request(low_risk, low_events))
        succeeded = asyncio.run(
            store.transition_request(
                low_risk.request_id,
                {ToolRequestState.RUNNING},
                ToolRequestState.SUCCEEDED,
                result={"iso": "2026-07-29T00:00:00+00:00"},
                event=ToolAuditEvent(
                    request_id=low_risk.request_id,
                    event_type="succeeded",
                ),
            )
        )

        self.assertEqual(succeeded.state, ToolRequestState.SUCCEEDED)
        self.assertEqual(
            succeeded.result,
            {"iso": "2026-07-29T00:00:00+00:00"},
        )

        asyncio.run(
            store.create_confirmation(request, confirmation, events)
        )
        cancelled = asyncio.run(
            store.cancel_request(
                request.request_id,
                datetime(2026, 7, 29, 0, 0, 1, tzinfo=timezone.utc),
            )
        )
        cancelled_confirmation = asyncio.run(
            store.get_confirmation(confirmation.confirmation_id)
        )

        self.assertEqual(cancelled.state, ToolRequestState.CANCELLED)
        self.assertEqual(
            cancelled_confirmation.state,
            ConfirmationState.CANCELLED,
        )

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

    def test_conversation_and_messages_round_trip_in_chronological_order(self):
        store = self.open_store()
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="desktop",
                owner_id="owner-1",
                title="Original title",
            )
        )
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="discord",
                owner_id="owner-2",
                title=None,
            )
        )

        with sqlite3.connect(self.database_path) as connection:
            conversation = connection.execute(
                "SELECT source, owner_id, title FROM conversations WHERE id = ?",
                ("conversation-1",),
            ).fetchone()

        self.assertEqual(
            conversation,
            ("discord", "owner-2", "Original title"),
        )

        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        messages = [
            StoredMessage(
                id="message-3",
                conversation_id="conversation-1",
                correlation_id="request-1",
                role="assistant",
                content="third",
                model="demo-model",
                status=MessageStatus.FAILED,
                created_at=created_at + timedelta(seconds=3),
            ),
            StoredMessage(
                id="message-1",
                conversation_id="conversation-1",
                correlation_id="request-1",
                role="user",
                content="first",
                created_at=created_at + timedelta(seconds=1),
            ),
            StoredMessage(
                id="message-2",
                conversation_id="conversation-1",
                correlation_id="request-1",
                role="assistant",
                content="second",
                model="demo-model",
                created_at=created_at + timedelta(seconds=2),
            ),
            StoredMessage(
                id="message-4",
                conversation_id="conversation-1",
                correlation_id="request-1",
                role="user",
                content="fourth",
                created_at=created_at + timedelta(seconds=4),
            ),
        ]
        for message in messages:
            asyncio.run(store.save_message(message))

        self.assertTrue(asyncio.run(store.has_message("message-1")))
        self.assertFalse(asyncio.run(store.has_message("missing-message")))

        listed = asyncio.run(store.list_messages("conversation-1"))
        recent = asyncio.run(store.recent_messages("conversation-1", 2))
        correlated = asyncio.run(
            store.find_assistant_by_correlation("request-1")
        )

        self.assertEqual(
            [message.id for message in listed],
            ["message-1", "message-2", "message-3", "message-4"],
        )
        self.assertEqual([message.id for message in recent], ["message-3", "message-4"])
        self.assertEqual(listed[2].status, MessageStatus.FAILED)
        self.assertEqual(listed[2].created_at, created_at + timedelta(seconds=3))
        self.assertEqual(correlated.id if correlated else None, "message-3")

        with self.assertRaises(ValueError):
            asyncio.run(store.recent_messages("conversation-1", 0))

    def test_claim_conversation_preserves_existing_scope_and_title(self):
        store = self.open_store()
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="desktop",
                owner_id="alice",
                title="Alice title",
            )
        )

        self.assertTrue(
            asyncio.run(
                store.claim_conversation(
                    "conversation-1",
                    source="desktop",
                    owner_id="alice",
                )
            )
        )
        self.assertFalse(
            asyncio.run(
                store.claim_conversation(
                    "conversation-1",
                    source="qq",
                    owner_id="bob",
                )
            )
        )

        with sqlite3.connect(self.database_path) as connection:
            conversation = connection.execute(
                "SELECT source, owner_id, title "
                "FROM conversations WHERE id = ?",
                ("conversation-1",),
            ).fetchone()

        self.assertEqual(conversation, ("desktop", "alice", "Alice title"))

    def test_claim_message_is_atomic_and_find_message_round_trips(self):
        store = self.open_store()
        self.assertTrue(
            asyncio.run(
                store.claim_conversation(
                    "conversation-1",
                    source="desktop",
                    owner_id="alice",
                )
            )
        )
        original = StoredMessage(
            id="message-1",
            conversation_id="conversation-1",
            role="user",
            content="original",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        conflicting = original.model_copy(
            update={
                "conversation_id": "conversation-2",
                "content": "changed",
            }
        )

        self.assertTrue(asyncio.run(store.claim_message(original)))
        self.assertFalse(asyncio.run(store.claim_message(conflicting)))
        found = asyncio.run(store.find_message(original.id))

        self.assertEqual(found, original)
        self.assertEqual(
            asyncio.run(store.list_messages("conversation-1")),
            [original],
        )

    def test_memory_upsert_and_deletes_are_scoped_by_source_and_owner(self):
        store = self.open_store()
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        original = MemoryItem(
            id="memory-original",
            source="desktop",
            owner_id="alice",
            content="Likes Tea",
            normalized_content="likes tea",
            source_message_id="message-1",
            created_at=created_at,
            updated_at=created_at,
        )
        replacement = MemoryItem(
            id="memory-replacement",
            source="desktop",
            owner_id="alice",
            content="Really likes tea",
            normalized_content="likes tea",
            source_message_id="message-2",
            created_at=created_at + timedelta(days=1),
            updated_at=created_at + timedelta(days=1),
        )
        other_owner = MemoryItem(
            id="memory-bob",
            source="desktop",
            owner_id="bob",
            content="Likes Tea",
            normalized_content="likes tea",
            created_at=created_at,
            updated_at=created_at,
        )

        first_saved = asyncio.run(store.save_memory(original))
        replacement_saved = asyncio.run(store.save_memory(replacement))
        asyncio.run(store.save_memory(other_owner))

        self.assertEqual(first_saved.id, "memory-original")
        self.assertEqual(replacement_saved.id, "memory-original")
        self.assertEqual(replacement_saved.content, "Really likes tea")
        self.assertEqual(replacement_saved.source_message_id, "message-2")
        self.assertEqual(replacement_saved.created_at, created_at)
        self.assertEqual(
            replacement_saved.updated_at,
            created_at + timedelta(days=1),
        )
        self.assertEqual(
            [item.id for item in asyncio.run(store.list_memories("desktop", "alice"))],
            ["memory-original"],
        )
        self.assertEqual(
            [item.id for item in asyncio.run(store.list_memories("desktop", "bob"))],
            ["memory-bob"],
        )

        self.assertFalse(
            asyncio.run(
                store.delete_memory_by_content("desktop", "charlie", "likes tea")
            )
        )
        self.assertFalse(
            asyncio.run(
                store.delete_memory_by_id(
                    "memory-original",
                    source="desktop",
                    owner_id="bob",
                )
            )
        )
        self.assertTrue(
            asyncio.run(
                store.delete_memory_by_content("desktop", "alice", "likes tea")
            )
        )
        self.assertEqual(
            asyncio.run(store.list_memories("desktop", "alice")),
            [],
        )
        self.assertEqual(
            [item.id for item in asyncio.run(store.list_memories("desktop", "bob"))],
            ["memory-bob"],
        )
        self.assertTrue(
            asyncio.run(
                store.delete_memory_by_id(
                    "memory-bob",
                    source="desktop",
                    owner_id="bob",
                )
            )
        )

    def test_model_call_accepts_arbitrary_status(self):
        store = self.open_store()
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="desktop",
                owner_id="owner-1",
            )
        )
        asyncio.run(
            store.save_message(
                StoredMessage(
                    id="message-1",
                    conversation_id="conversation-1",
                    role="user",
                    content="request",
                )
            )
        )
        record = ModelCallRecord(
            id="call-1",
            message_id="message-1",
            model="demo-model",
            status="timeout_error",
            latency_ms=125,
            prompt_tokens=10,
            completion_tokens=4,
            provider_request_id="provider-1",
        )

        asyncio.run(store.record_model_call(record))

        with sqlite3.connect(self.database_path) as connection:
            stored = connection.execute(
                "SELECT status, latency_ms, prompt_tokens, completion_tokens, "
                "provider_request_id FROM model_calls WHERE id = ?",
                ("call-1",),
            ).fetchone()

        self.assertEqual(stored, ("timeout_error", 125, 10, 4, "provider-1"))

    def test_save_model_result_rolls_back_assistant_when_model_call_insert_fails(self):
        store = self.open_store()
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="desktop",
                owner_id="owner-1",
            )
        )
        user_message = StoredMessage(
            id="user-1",
            conversation_id="conversation-1",
            role="user",
            content="request",
        )
        asyncio.run(store.save_message(user_message))
        existing_record = ModelCallRecord(
            id="call-rollback",
            message_id="user-1",
            model="original-model",
            status="succeeded",
            latency_ms=10,
            provider_request_id="original-request",
        )
        asyncio.run(store.record_model_call(existing_record))
        with sqlite3.connect(self.database_path) as connection:
            original_record = connection.execute(
                "SELECT * FROM model_calls WHERE id = ?",
                ("call-rollback",),
            ).fetchone()
        assistant = StoredMessage(
            id="assistant-rollback",
            conversation_id="conversation-1",
            correlation_id="user-1",
            role="assistant",
            content="new response",
        )
        duplicate_record = ModelCallRecord(
            id="call-rollback",
            message_id="user-1",
            model="duplicate-model",
            status="timeout_error",
            latency_ms=99,
            provider_request_id="duplicate-request",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            asyncio.run(store.save_model_result(duplicate_record, assistant))

        with sqlite3.connect(self.database_path) as connection:
            assistant_count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE id = ?",
                ("assistant-rollback",),
            ).fetchone()[0]
            stored_record = connection.execute(
                "SELECT * FROM model_calls WHERE id = ?",
                ("call-rollback",),
            ).fetchone()

        self.assertEqual(assistant_count, 0)
        self.assertEqual(stored_record, original_record)

    def test_delete_conversation_cascades_messages_and_model_calls(self):
        store = self.open_store()
        asyncio.run(
            store.upsert_conversation(
                "conversation-1",
                source="desktop",
                owner_id="owner-1",
            )
        )
        user_message = StoredMessage(
            id="user-1",
            conversation_id="conversation-1",
            role="user",
            content="request",
        )
        asyncio.run(store.save_message(user_message))
        assistant = StoredMessage(
            id="assistant-1",
            conversation_id="conversation-1",
            correlation_id="user-1",
            role="assistant",
            content="hello",
        )
        record = ModelCallRecord(
            id="call-1",
            message_id="user-1",
            model="demo-model",
            status="succeeded",
            latency_ms=10,
        )
        asyncio.run(store.save_model_result(record, assistant))

        self.assertTrue(asyncio.run(store.delete_conversation("conversation-1")))
        self.assertFalse(asyncio.run(store.delete_conversation("conversation-1")))

        with sqlite3.connect(self.database_path) as connection:
            message_count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                ("conversation-1",),
            ).fetchone()[0]
            model_call_count = connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE message_id = ?",
                ("user-1",),
            ).fetchone()[0]

        self.assertEqual(message_count, 0)
        self.assertEqual(model_call_count, 0)

    def test_store_satisfies_all_repository_protocols_at_runtime(self):
        store = self.open_store()

        self.assertIsInstance(store, ConversationRepository)
        self.assertIsInstance(store, MemoryRepository)
        self.assertIsInstance(store, ModelCallRepository)
        self.assertIsInstance(store, ToolRepository)


if __name__ == "__main__":
    unittest.main()
