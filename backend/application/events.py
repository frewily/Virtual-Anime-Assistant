import asyncio
import logging
from collections.abc import Awaitable, Callable

from domain.responses import AssistantResponse

logger = logging.getLogger(__name__)
ResponseSubscriber = Callable[[AssistantResponse], Awaitable[None]]


class ResponsePublisher:
    def __init__(self):
        self._subscribers: list[ResponseSubscriber] = []

    def subscribe(self, subscriber: ResponseSubscriber) -> Callable[[], None]:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    async def publish(self, response: AssistantResponse) -> None:
        results = await asyncio.gather(
            *(subscriber(response) for subscriber in tuple(self._subscribers)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "Response subscriber failed: %s",
                    type(result).__name__,
                )
