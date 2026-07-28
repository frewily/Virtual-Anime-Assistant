import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from domain.messages import IncomingMessage
from domain.responses import AssistantResponse


@dataclass
class SessionState:
    conversation_id: str
    turn_count: int = 0
    last_message_id: str | None = None


SessionHandler = Callable[
    [IncomingMessage, SessionState],
    Awaitable[AssistantResponse],
]


class SessionRegistry:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, SessionState] = {}

    async def run(
        self,
        message: IncomingMessage,
        handler: SessionHandler,
    ) -> AssistantResponse:
        lock = self._locks.setdefault(message.conversation_id, asyncio.Lock())
        state = self._states.setdefault(
            message.conversation_id,
            SessionState(conversation_id=message.conversation_id),
        )
        async with lock:
            response = await handler(message, state)
            state.turn_count += 1
            state.last_message_id = message.message_id
            return response

    def get_state(self, conversation_id: str) -> SessionState | None:
        return self._states.get(conversation_id)
