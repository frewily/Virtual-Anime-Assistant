"""OneBot reverse WebSocket authentication and connection lifecycle."""

import asyncio
import hmac
import re
from collections.abc import Mapping
from uuid import uuid4

from channels.onebot.config import OneBotSettings
from channels.onebot.models import (
    ONEBOT_ACTION_FAILED,
    ONEBOT_ACTION_TIMEOUT,
    ONEBOT_AUTHENTICATION_FAILED,
    ONEBOT_DISCONNECTED,
    ONEBOT_DUPLICATE_CONNECTION,
    OneBotAction,
    OneBotChannelError,
)


_POSITIVE_DECIMAL = re.compile(r"^[0-9]+$")


def authenticate_onebot(
    authorization: str | None,
    self_id_header: str | None,
    settings: OneBotSettings,
) -> int:
    if authorization is None or not authorization.startswith("Bearer "):
        raise OneBotChannelError(ONEBOT_AUTHENTICATION_FAILED)

    candidate = authorization[len("Bearer ") :]
    if (
        not candidate
        or candidate.strip() != candidate
        or not hmac.compare_digest(candidate, settings.access_token)
    ):
        raise OneBotChannelError(ONEBOT_AUTHENTICATION_FAILED)

    if (
        self_id_header is None
        or _POSITIVE_DECIMAL.fullmatch(self_id_header) is None
    ):
        raise OneBotChannelError(ONEBOT_AUTHENTICATION_FAILED)
    self_id = int(self_id_header)
    if self_id <= 0:
        raise OneBotChannelError(ONEBOT_AUTHENTICATION_FAILED)
    return self_id


class OneBotConnectionManager:
    def __init__(self, *, action_timeout_seconds: float) -> None:
        self._action_timeout_seconds = action_timeout_seconds
        self._lock = asyncio.Lock()
        self._websocket: object | None = None
        self._self_id: int | None = None
        self._pending: dict[
            str,
            asyncio.Future[dict[str, object]],
        ] = {}

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def self_id(self) -> int | None:
        return self._self_id

    @property
    def pending_action_count(self) -> int:
        return len(self._pending)

    async def attach(self, websocket: object, self_id: int) -> None:
        async with self._lock:
            if self._websocket is not None:
                raise OneBotChannelError(ONEBOT_DUPLICATE_CONNECTION)
            self._websocket = websocket
            self._self_id = self_id

    async def detach(self, websocket: object) -> None:
        async with self._lock:
            if self._websocket is not websocket:
                return
            self._websocket = None
            self._self_id = None
            pending = tuple(self._pending.values())
            self._pending.clear()

        for future in pending:
            if not future.done():
                future.set_exception(
                    OneBotChannelError(ONEBOT_DISCONNECTED)
                )

    async def send_action(self, action: OneBotAction) -> None:
        loop = asyncio.get_running_loop()
        echo = uuid4().hex
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        async with self._lock:
            websocket = self._websocket
            if websocket is None:
                raise OneBotChannelError(ONEBOT_DISCONNECTED)
            self._pending[echo] = future

        payload = {
            "action": action.action,
            "params": action.params,
            "echo": echo,
        }
        try:
            try:
                await websocket.send_json(payload)
            except Exception as exc:
                async with self._lock:
                    self._pending.pop(echo, None)
                await self.detach(websocket)
                raise OneBotChannelError(ONEBOT_DISCONNECTED) from exc

            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=self._action_timeout_seconds,
                )
            except TimeoutError as exc:
                raise OneBotChannelError(ONEBOT_ACTION_TIMEOUT) from exc

            retcode = response.get("retcode")
            if (
                response.get("status") != "ok"
                or isinstance(retcode, bool)
                or retcode != 0
            ):
                raise OneBotChannelError(ONEBOT_ACTION_FAILED)
        finally:
            async with self._lock:
                self._pending.pop(echo, None)

    def resolve_action_response(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        echo = payload.get("echo")
        if not isinstance(echo, str):
            return False
        future = self._pending.get(echo)
        if future is None or future.done():
            return False
        future.set_result(dict(payload))
        return True

    async def aclose(self) -> None:
        async with self._lock:
            websocket = self._websocket
        if websocket is None:
            return
        try:
            await websocket.close(
                code=1001,
                reason=ONEBOT_DISCONNECTED,
            )
        finally:
            await self.detach(websocket)
