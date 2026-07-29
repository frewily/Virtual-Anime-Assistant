import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import WebSocketDisconnect

from api.app import create_app
from api.qq import get_qq_status, qq_websocket
from application.assistant import AssistantApplication
from application.context import ConversationContextBuilder
from application.events import ResponsePublisher
from application.model_tools import ModelToolOrchestrator
from channels.onebot.config import OneBotSettings
from channels.onebot.connection import OneBotConnectionManager
from channels.onebot.models import (
    ONEBOT_AUTHENTICATION_FAILED,
    ONEBOT_DUPLICATE_CONNECTION,
    QQ_DISABLED,
    QQ_MISCONFIGURED,
    OneBotAction,
    OneBotChannelError,
)
from core.runtime import AssistantRuntime
from infrastructure.sqlite_store import SqliteStore
from llm.models import ModelReply, ModelRequest, ModelToolCall
from tools.builtin import build_builtin_registry
from tools.catalog import ModelToolCatalog
from tools.service import ToolExecutionService


def ready_settings() -> OneBotSettings:
    return OneBotSettings(
        enabled=True,
        access_token="0123456789abcdef",
        allowed_group_ids=frozenset({789}),
        allowed_user_ids=frozenset({456, 457}),
        rate_per_minute=10,
        rate_burst=2,
        max_concurrency=4,
        action_timeout_seconds=10,
    )


class FakeConnection:
    def __init__(self, *, connected: bool = False) -> None:
        self.connected = connected
        self.attach = AsyncMock()
        self.detach = AsyncMock()
        self.resolve_action_response = Mock(return_value=False)


class RecordingConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.actions = []
        self.send_action = AsyncMock(side_effect=self._send)
        self.aclose = AsyncMock()

    async def _send(self, action) -> None:
        self.actions.append(action)


class QueuedFakeGateway:
    model_name = "fake-model"

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []
        self.complete = AsyncMock(side_effect=self._complete)

    async def _complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("fake model reply queue exhausted")
        return self.replies.pop(0)


class FakeWebSocket:
    def __init__(
        self,
        runtime,
        *,
        authorization: str | None = "Bearer 0123456789abcdef",
        self_id: str | None = "123",
        frames: tuple[object, ...] = (),
    ) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
        self.headers = {}
        if authorization is not None:
            self.headers["authorization"] = authorization
        if self_id is not None:
            self.headers["x-self-id"] = self_id
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self._frames = iter((*frames, WebSocketDisconnect()))
        self.receive_text = AsyncMock(side_effect=self._receive_text)

    async def _receive_text(self):
        await asyncio.sleep(0)
        frame = next(self._frames)
        if isinstance(frame, BaseException):
            raise frame
        return frame


class QueuedWebSocket:
    DISCONNECT = object()

    def __init__(self, runtime) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(runtime=runtime)
        )
        self.headers = {
            "authorization": "Bearer 0123456789abcdef",
            "x-self-id": "123",
        }
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_json = AsyncMock(side_effect=self._send_json)
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def _send_json(self, payload: dict[str, object]) -> None:
        await self.sent.put(payload)

    async def receive_text(self) -> str:
        frame = await self._incoming.get()
        if frame is self.DISCONNECT:
            raise WebSocketDisconnect()
        return str(frame)

    async def push_json(self, payload: dict[str, object]) -> None:
        await self._incoming.put(json.dumps(payload))

    async def disconnect(self) -> None:
        await self._incoming.put(self.DISCONNECT)


def runtime_for(
    settings: OneBotSettings,
    *,
    connection: FakeConnection | None = None,
    channel=None,
):
    return SimpleNamespace(
        qq_settings=settings,
        qq_connection=connection or FakeConnection(),
        qq_channel=channel or SimpleNamespace(handle_event=AsyncMock()),
    )


class OneBotStatusApiTests(unittest.TestCase):
    def test_status_only_exposes_safe_counts_and_connection_state(self):
        connection = FakeConnection()
        runtime = runtime_for(ready_settings(), connection=connection)

        self.assertEqual(
            get_qq_status(runtime),
            {
                "enabled": True,
                "state": "disconnected",
                "allowedGroupCount": 1,
                "allowedUserCount": 2,
            },
        )
        connection.connected = True
        connected = get_qq_status(runtime)
        self.assertEqual(connected["state"], "connected")
        serialized = json.dumps(connected)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("0123456789abcdef", serialized)
        self.assertNotIn("789", serialized)
        self.assertNotIn("456", serialized)

    def test_disabled_and_misconfigured_states_are_distinct(self):
        disabled = OneBotSettings()
        misconfigured = OneBotSettings(
            enabled=True,
            configuration_error=QQ_MISCONFIGURED,
        )

        self.assertEqual(
            get_qq_status(runtime_for(disabled))["state"],
            "disabled",
        )
        self.assertEqual(
            get_qq_status(runtime_for(misconfigured))["state"],
            "misconfigured",
        )

    def test_app_registers_http_and_websocket_qq_routes(self):
        paths = {
            route.path
            for route in create_app(runtime_for(OneBotSettings())).routes
        }

        self.assertIn("/api/qq/status", paths)
        self.assertIn("/ws/qq", paths)


class OneBotApplicationIntegrationTests(unittest.TestCase):
    @staticmethod
    def _time_tool_reply(call_id: str) -> ModelReply:
        return ModelReply(
            text=None,
            tool_calls=[
                ModelToolCall(
                    id=call_id,
                    name="system.current_time",
                    arguments={"timezone": "UTC"},
                )
            ],
            model="fake-model",
        )

    @staticmethod
    def _build_tool_runtime(
        database_path: Path,
        replies: list[ModelReply],
    ):
        store = SqliteStore(database_path)
        gateway = QueuedFakeGateway(replies)
        registry = build_builtin_registry()
        tool_service = ToolExecutionService(
            registry=registry,
            repository=store,
        )
        catalog = ModelToolCatalog(registry)
        orchestrator = ModelToolOrchestrator(
            gateway=gateway,
            catalog=catalog,
            tool_service=tool_service,
            enabled=True,
        )
        tts = Mock()
        tts.synthesize = AsyncMock(return_value=None)
        publisher = ResponsePublisher()
        desktop_subscriber = AsyncMock()
        publisher.subscribe(desktop_subscriber)
        application = AssistantApplication(
            tts=tts,
            llm=gateway,
            store=store,
            context_builder=ConversationContextBuilder(20, 12000),
            publisher=publisher,
            model_orchestrator=orchestrator,
        )
        connection = RecordingConnection()
        runtime = AssistantRuntime(
            application=application,
            store=store,
            tool_registry=registry,
            tool_service=tool_service,
            qq_settings=ready_settings(),
            qq_connection=connection,
        )
        runtime.model_tool_catalog = catalog
        runtime.model_tool_orchestrator = orchestrator
        return (
            runtime,
            store,
            gateway,
            application,
            connection,
            desktop_subscriber,
        )

    def test_qq_uses_real_application_sqlite_and_does_not_publish_desktop(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore(Path(directory) / "assistant.db")
            llm = Mock()
            llm.model_name = "fake-model"
            llm.complete = AsyncMock(
                return_value=ModelReply(
                    text="真实应用回复",
                    model="fake-model",
                )
            )
            tts = Mock()
            tts.synthesize = AsyncMock(return_value=None)
            publisher = ResponsePublisher()
            desktop_subscriber = AsyncMock()
            publisher.subscribe(desktop_subscriber)
            application = AssistantApplication(
                tts=tts,
                llm=llm,
                store=store,
                context_builder=ConversationContextBuilder(20, 12000),
                publisher=publisher,
            )
            connection = RecordingConnection()
            runtime = AssistantRuntime(
                application=application,
                store=store,
                qq_settings=ready_settings(),
                qq_connection=connection,
            )
            payload = {
                "post_type": "message",
                "message_type": "private",
                "self_id": 123,
                "user_id": 456,
                "message_id": 900,
                "message": "你好",
            }

            asyncio.run(
                runtime.qq_channel.handle_event(payload, self_id=123)
            )
            asyncio.run(
                runtime.qq_channel.handle_event(payload, self_id=123)
            )

            self.assertEqual(llm.complete.await_count, 1)
            desktop_subscriber.assert_not_awaited()
            self.assertEqual(len(connection.actions), 1)
            self.assertEqual(
                connection.actions[0].action,
                "send_private_msg",
            )
            stored = asyncio.run(store.find_message("qq:123:900"))
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored.conversation_id,
                "qq:private:456",
            )
            asyncio.run(runtime.aclose())
            asyncio.run(store.close())

    def test_qq_trigger_rules_reuse_model_tool_orchestrator_and_deduplicate(
        self,
    ):
        replies = [
            self._time_tool_reply("private-time"),
            ModelReply(text="QQ 私聊时间回复", model="fake-model"),
            self._time_tool_reply("group-time"),
            ModelReply(text="QQ 群聊时间回复", model="fake-model"),
        ]
        private_event = {
            "post_type": "message",
            "message_type": "private",
            "self_id": 123,
            "user_id": 456,
            "message_id": 901,
            "message": "现在几点？",
        }
        unmentioned_group_event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 123,
            "user_id": 457,
            "group_id": 789,
            "message_id": 902,
            "message": [
                {"type": "text", "data": {"text": "现在几点？"}},
            ],
        }
        mentioned_group_event = {
            **unmentioned_group_event,
            "message_id": 903,
            "message": [
                {"type": "at", "data": {"qq": "123"}},
                {"type": "text", "data": {"text": " 现在几点？"}},
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            (
                runtime,
                store,
                gateway,
                application,
                connection,
                desktop_subscriber,
            ) = self._build_tool_runtime(
                Path(directory) / "assistant.db",
                replies,
            )

            asyncio.run(
                runtime.qq_channel.handle_event(private_event, self_id=123)
            )
            asyncio.run(
                runtime.qq_channel.handle_event(private_event, self_id=123)
            )
            asyncio.run(
                runtime.qq_channel.handle_event(
                    unmentioned_group_event,
                    self_id=123,
                )
            )
            asyncio.run(
                runtime.qq_channel.handle_event(
                    mentioned_group_event,
                    self_id=123,
                )
            )
            asyncio.run(
                runtime.qq_channel.handle_event(
                    mentioned_group_event,
                    self_id=123,
                )
            )

            self.assertIs(
                application.model_orchestrator,
                runtime.model_tool_orchestrator,
            )
            self.assertEqual(gateway.complete.await_count, 4)
            self.assertEqual(len(gateway.requests), 4)
            self.assertEqual(gateway.replies, [])
            self.assertEqual(len(connection.actions), 2)
            private_reply, group_reply = connection.actions
            self.assertEqual(private_reply.action, "send_private_msg")
            self.assertEqual(private_reply.params["user_id"], 456)
            self.assertEqual(
                private_reply.params["message"],
                [
                    {
                        "type": "text",
                        "data": {"text": "QQ 私聊时间回复"},
                    }
                ],
            )
            self.assertEqual(group_reply.action, "send_group_msg")
            self.assertEqual(group_reply.params["group_id"], 789)
            self.assertEqual(
                group_reply.params["message"],
                [
                    {"type": "reply", "data": {"id": "903"}},
                    {"type": "at", "data": {"qq": "457"}},
                    {
                        "type": "text",
                        "data": {"text": "QQ 群聊时间回复"},
                    },
                ],
            )
            desktop_subscriber.assert_not_awaited()
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection_db:
                tool_rows = connection_db.execute(
                    "SELECT source, state FROM tool_requests "
                    "ORDER BY created_at"
                ).fetchall()
                model_call_count = connection_db.execute(
                    "SELECT COUNT(*) FROM model_calls"
                ).fetchone()[0]
                confirmation_count = connection_db.execute(
                    "SELECT COUNT(*) FROM tool_confirmations"
                ).fetchone()[0]
            self.assertEqual(
                tool_rows,
                [("model", "succeeded"), ("model", "succeeded")],
            )
            self.assertEqual(model_call_count, 4)
            self.assertEqual(confirmation_count, 0)

            asyncio.run(runtime.aclose())
            asyncio.run(store.close())


class OneBotWebSocketApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_response_resolves_while_event_is_running(self):
        configured = replace(
            ready_settings(),
            action_timeout_seconds=0.05,
        )
        connection = OneBotConnectionManager(
            action_timeout_seconds=configured.action_timeout_seconds
        )
        completed = asyncio.Event()

        async def handle_event(payload, *, self_id):
            await connection.send_action(
                OneBotAction(
                    "send_private_msg",
                    {"user_id": 456, "message": []},
                )
            )
            completed.set()

        runtime = runtime_for(
            configured,
            connection=connection,
            channel=SimpleNamespace(handle_event=handle_event),
        )
        websocket = QueuedWebSocket(runtime)
        route = asyncio.create_task(qq_websocket(websocket))

        await websocket.push_json({"post_type": "message"})
        action = await asyncio.wait_for(websocket.sent.get(), 0.1)
        await websocket.push_json(
            {
                "status": "ok",
                "retcode": 0,
                "echo": action["echo"],
            }
        )

        await asyncio.wait_for(completed.wait(), 0.1)
        await websocket.disconnect()
        await asyncio.wait_for(route, 0.1)

        self.assertEqual(connection.pending_action_count, 0)

    async def test_disabled_misconfigured_and_bad_auth_are_rejected(self):
        cases = (
            (
                OneBotSettings(),
                "Bearer 0123456789abcdef",
                "123",
                QQ_DISABLED,
            ),
            (
                OneBotSettings(
                    enabled=True,
                    configuration_error=QQ_MISCONFIGURED,
                ),
                "Bearer 0123456789abcdef",
                "123",
                QQ_MISCONFIGURED,
            ),
            (
                ready_settings(),
                None,
                "123",
                ONEBOT_AUTHENTICATION_FAILED,
            ),
            (
                ready_settings(),
                "Bearer wrong-token-value",
                "123",
                ONEBOT_AUTHENTICATION_FAILED,
            ),
            (
                ready_settings(),
                "Bearer 0123456789abcdef",
                "not-an-id",
                ONEBOT_AUTHENTICATION_FAILED,
            ),
        )

        for settings, authorization, self_id, error_code in cases:
            with self.subTest(error_code=error_code):
                runtime = runtime_for(settings)
                websocket = FakeWebSocket(
                    runtime,
                    authorization=authorization,
                    self_id=self_id,
                )

                await qq_websocket(websocket)

                websocket.accept.assert_not_awaited()
                websocket.close.assert_awaited_once_with(
                    code=1008,
                    reason=error_code,
                )
                runtime.qq_connection.attach.assert_not_awaited()

    async def test_duplicate_connection_is_rejected_without_accept(self):
        connection = FakeConnection()
        connection.attach.side_effect = OneBotChannelError(
            ONEBOT_DUPLICATE_CONNECTION
        )
        runtime = runtime_for(
            ready_settings(),
            connection=connection,
        )
        websocket = FakeWebSocket(runtime)

        await qq_websocket(websocket)

        websocket.accept.assert_not_awaited()
        websocket.close.assert_awaited_once_with(
            code=1008,
            reason=ONEBOT_DUPLICATE_CONNECTION,
        )
        connection.detach.assert_not_awaited()

    async def test_valid_objects_route_echo_before_channel_events(self):
        connection = FakeConnection()
        connection.resolve_action_response.side_effect = [True, False]
        channel = SimpleNamespace(handle_event=AsyncMock())
        runtime = runtime_for(
            ready_settings(),
            connection=connection,
            channel=channel,
        )
        websocket = FakeWebSocket(
            runtime,
            frames=(
                json.dumps({"echo": "known", "status": "ok"}),
                json.dumps({"post_type": "message"}),
            ),
        )

        await qq_websocket(websocket)

        websocket.accept.assert_awaited_once_with()
        connection.attach.assert_awaited_once_with(websocket, 123)
        self.assertEqual(
            connection.resolve_action_response.call_count,
            2,
        )
        channel.handle_event.assert_awaited_once_with(
            {"post_type": "message"},
            self_id=123,
        )
        connection.detach.assert_awaited_once_with(websocket)

    async def test_third_consecutive_invalid_frame_closes_as_unsupported(self):
        runtime = runtime_for(ready_settings())
        websocket = FakeWebSocket(
            runtime,
            frames=("not-json", "[]", "null", json.dumps({})),
        )

        await qq_websocket(websocket)

        websocket.close.assert_awaited_once_with(
            code=1003,
            reason="onebot_invalid_event",
        )
        self.assertEqual(websocket.receive_text.await_count, 3)
        runtime.qq_connection.detach.assert_awaited_once_with(websocket)

    async def test_valid_object_resets_invalid_frame_counter(self):
        runtime = runtime_for(ready_settings())
        websocket = FakeWebSocket(
            runtime,
            frames=(
                "not-json",
                json.dumps({}),
                "[]",
                "null",
            ),
        )

        await qq_websocket(websocket)

        websocket.close.assert_not_awaited()
        runtime.qq_channel.handle_event.assert_awaited_once_with(
            {},
            self_id=123,
        )

    async def test_channel_failure_isolated_from_following_event(self):
        second_completed = asyncio.Event()
        calls = 0

        async def handle_event(payload, *, self_id):
            nonlocal calls
            calls += 1
            if payload == {"event": 1}:
                raise RuntimeError("private failure")
            second_completed.set()

        runtime = runtime_for(
            ready_settings(),
            channel=SimpleNamespace(handle_event=handle_event),
        )
        websocket = QueuedWebSocket(runtime)
        route = asyncio.create_task(qq_websocket(websocket))

        await websocket.push_json({"event": 1})
        await websocket.push_json({"event": 2})
        await asyncio.wait_for(second_completed.wait(), 0.1)
        await websocket.disconnect()
        await asyncio.wait_for(route, 0.1)

        self.assertEqual(calls, 2)
        websocket.close.assert_not_awaited()

    async def test_disconnect_cancels_and_reaps_running_event_tasks(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle_event(payload, *, self_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime = runtime_for(
            ready_settings(),
            channel=SimpleNamespace(handle_event=handle_event),
        )
        websocket = QueuedWebSocket(runtime)
        route = asyncio.create_task(qq_websocket(websocket))

        await websocket.push_json({"post_type": "message"})
        await asyncio.wait_for(started.wait(), 0.1)
        await websocket.disconnect()
        await asyncio.wait_for(route, 0.1)

        await asyncio.wait_for(cancelled.wait(), 0.1)
        runtime.qq_connection.detach.assert_awaited_once_with(websocket)


if __name__ == "__main__":
    unittest.main()
