import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.onebot.config import OneBotSettings
from channels.onebot.connection import (
    OneBotConnectionManager,
    authenticate_onebot,
)
from channels.onebot.models import (
    ONEBOT_ACTION_FAILED,
    ONEBOT_ACTION_TIMEOUT,
    ONEBOT_AUTHENTICATION_FAILED,
    ONEBOT_DISCONNECTED,
    ONEBOT_DUPLICATE_CONNECTION,
    OneBotAction,
    OneBotChannelError,
)


def settings() -> OneBotSettings:
    return OneBotSettings(
        enabled=True,
        access_token="0123456789abcdef",
        allowed_group_ids=frozenset({789}),
        allowed_user_ids=frozenset({456}),
        rate_per_minute=10,
        rate_burst=2,
        max_concurrency=4,
        action_timeout_seconds=10,
    )


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.send_json = AsyncMock(side_effect=self._send)
        self.close = AsyncMock()

    async def _send(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


class OneBotAuthenticationTests(unittest.TestCase):
    def test_valid_bearer_token_returns_positive_self_id(self):
        with patch(
            "channels.onebot.connection.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as compare:
            self_id = authenticate_onebot(
                "Bearer 0123456789abcdef",
                "123456",
                settings(),
            )

        self.assertEqual(self_id, 123456)
        compare.assert_called_once_with(
            "0123456789abcdef",
            "0123456789abcdef",
        )

    def test_missing_wrong_scheme_and_wrong_token_are_rejected(self):
        headers = (
            None,
            "Basic 0123456789abcdef",
            "bearer 0123456789abcdef",
            "Bearer wrong-token-value",
            "Bearer  0123456789abcdef",
        )

        for authorization in headers:
            with self.subTest(authorization=authorization):
                with self.assertRaises(OneBotChannelError) as raised:
                    authenticate_onebot(
                        authorization,
                        "123",
                        settings(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    ONEBOT_AUTHENTICATION_FAILED,
                )

    def test_self_id_must_be_a_positive_decimal_integer(self):
        for self_id in (None, "", "0", "-1", "+1", "1.5", "abc"):
            with self.subTest(self_id=self_id):
                with self.assertRaises(OneBotChannelError) as raised:
                    authenticate_onebot(
                        "Bearer 0123456789abcdef",
                        self_id,
                        settings(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    ONEBOT_AUTHENTICATION_FAILED,
                )

    def test_authentication_error_never_contains_headers_or_token(self):
        secret = "private-token-that-must-not-leak"
        configured = settings()
        configured = OneBotSettings(
            enabled=configured.enabled,
            access_token=secret,
            allowed_group_ids=configured.allowed_group_ids,
            allowed_user_ids=configured.allowed_user_ids,
            rate_per_minute=configured.rate_per_minute,
            rate_burst=configured.rate_burst,
            max_concurrency=configured.max_concurrency,
            action_timeout_seconds=configured.action_timeout_seconds,
        )

        with self.assertRaises(OneBotChannelError) as raised:
            authenticate_onebot(
                "Bearer attacker-controlled-token",
                "invalid-self-id",
                configured,
            )

        rendered = str(raised.exception)
        self.assertEqual(rendered, ONEBOT_AUTHENTICATION_FAILED)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("attacker-controlled-token", rendered)


class OneBotConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_active_connection_is_allowed(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        first = FakeSocket()
        second = FakeSocket()

        await manager.attach(first, 123)
        with self.assertRaises(OneBotChannelError) as raised:
            await manager.attach(second, 456)

        self.assertEqual(
            raised.exception.code,
            ONEBOT_DUPLICATE_CONNECTION,
        )
        self.assertTrue(manager.connected)
        self.assertEqual(manager.self_id, 123)

        await manager.detach(first)
        await manager.attach(second, 456)
        self.assertEqual(manager.self_id, 456)

    async def test_matching_echo_completes_action(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        socket = FakeSocket()
        await manager.attach(socket, 123)

        pending = asyncio.create_task(
            manager.send_action(
                OneBotAction(
                    action="send_private_msg",
                    params={"user_id": 456, "message": []},
                )
            )
        )
        await asyncio.sleep(0)
        payload = socket.sent[0]

        resolved = manager.resolve_action_response(
            {
                "status": "ok",
                "retcode": 0,
                "echo": payload["echo"],
                "data": {"message_id": 999},
            }
        )

        self.assertTrue(resolved)
        await pending
        self.assertEqual(socket.send_json.await_count, 1)

    async def test_unknown_echo_is_ignored(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        socket = FakeSocket()
        await manager.attach(socket, 123)
        pending = asyncio.create_task(
            manager.send_action(OneBotAction("example", {}))
        )
        await asyncio.sleep(0)
        known_echo = socket.sent[0]["echo"]

        self.assertFalse(
            manager.resolve_action_response(
                {"status": "ok", "retcode": 0, "echo": "unknown"}
            )
        )
        self.assertFalse(pending.done())
        self.assertTrue(
            manager.resolve_action_response(
                {"status": "ok", "retcode": 0, "echo": known_echo}
            )
        )
        await pending

    async def test_failed_action_uses_stable_error_without_response_body(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        socket = FakeSocket()
        await manager.attach(socket, 123)
        pending = asyncio.create_task(
            manager.send_action(OneBotAction("example", {}))
        )
        await asyncio.sleep(0)
        echo = socket.sent[0]["echo"]
        manager.resolve_action_response(
            {
                "status": "failed",
                "retcode": 1404,
                "echo": echo,
                "message": "private OneBot response body",
            }
        )

        with self.assertRaises(OneBotChannelError) as raised:
            await pending

        self.assertEqual(raised.exception.code, ONEBOT_ACTION_FAILED)
        self.assertEqual(str(raised.exception), ONEBOT_ACTION_FAILED)

    async def test_action_timeout_does_not_retry(self):
        manager = OneBotConnectionManager(action_timeout_seconds=0.01)
        socket = FakeSocket()
        await manager.attach(socket, 123)

        with self.assertRaises(OneBotChannelError) as raised:
            await manager.send_action(OneBotAction("example", {}))

        self.assertEqual(raised.exception.code, ONEBOT_ACTION_TIMEOUT)
        self.assertEqual(socket.send_json.await_count, 1)
        self.assertEqual(manager.pending_action_count, 0)

    async def test_disconnect_fails_pending_actions_and_allows_reconnect(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        first = FakeSocket()
        second = FakeSocket()
        await manager.attach(first, 123)
        pending = asyncio.create_task(
            manager.send_action(OneBotAction("example", {}))
        )
        await asyncio.sleep(0)

        await manager.detach(first)

        with self.assertRaises(OneBotChannelError) as raised:
            await pending
        self.assertEqual(raised.exception.code, ONEBOT_DISCONNECTED)
        self.assertEqual(manager.pending_action_count, 0)
        await manager.attach(second, 456)
        self.assertTrue(manager.connected)

    async def test_late_detach_cannot_clear_replacement_connection(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        first = FakeSocket()
        second = FakeSocket()
        await manager.attach(first, 123)
        await manager.detach(first)
        await manager.attach(second, 456)

        await manager.detach(first)

        self.assertTrue(manager.connected)
        self.assertEqual(manager.self_id, 456)

    async def test_send_failure_becomes_disconnected_without_leaking_details(
        self,
    ):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        socket = FakeSocket()
        socket.send_json.side_effect = RuntimeError(
            "private websocket failure"
        )
        await manager.attach(socket, 123)

        with self.assertRaises(OneBotChannelError) as raised:
            await manager.send_action(OneBotAction("example", {}))

        self.assertEqual(raised.exception.code, ONEBOT_DISCONNECTED)
        self.assertEqual(str(raised.exception), ONEBOT_DISCONNECTED)
        self.assertEqual(manager.pending_action_count, 0)

    async def test_close_is_idempotent_and_closes_active_socket(self):
        manager = OneBotConnectionManager(action_timeout_seconds=1)
        socket = FakeSocket()
        await manager.attach(socket, 123)

        await manager.aclose()
        await manager.aclose()

        socket.close.assert_awaited_once()
        self.assertFalse(manager.connected)


if __name__ == "__main__":
    unittest.main()
