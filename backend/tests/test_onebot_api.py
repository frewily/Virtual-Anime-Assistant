import asyncio
import json
import sys
import tempfile
import unittest
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
from channels.onebot.config import OneBotSettings
from channels.onebot.models import (
    ONEBOT_AUTHENTICATION_FAILED,
    ONEBOT_DUPLICATE_CONNECTION,
    QQ_DISABLED,
    QQ_MISCONFIGURED,
    OneBotChannelError,
)
from core.runtime import AssistantRuntime
from infrastructure.sqlite_store import SqliteStore
from llm.models import ModelReply


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
        self.receive_text = AsyncMock(
            side_effect=(*frames, WebSocketDisconnect())
        )


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


class OneBotWebSocketApiTests(unittest.IsolatedAsyncioTestCase):
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
        channel = SimpleNamespace(
            handle_event=AsyncMock(
                side_effect=[RuntimeError("private failure"), None]
            )
        )
        runtime = runtime_for(ready_settings(), channel=channel)
        websocket = FakeWebSocket(
            runtime,
            frames=(
                json.dumps({"event": 1}),
                json.dumps({"event": 2}),
            ),
        )

        await qq_websocket(websocket)

        self.assertEqual(channel.handle_event.await_count, 2)
        websocket.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
