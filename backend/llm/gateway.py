from typing import Protocol, runtime_checkable

from .models import ModelReply, ModelRequest


@runtime_checkable
class LanguageModelGateway(Protocol):
    @property
    def model_name(self) -> str: ...

    async def complete(self, request: ModelRequest) -> ModelReply: ...
