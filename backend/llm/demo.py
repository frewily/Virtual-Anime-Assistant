from .models import ModelReply, ModelRequest


class DemoLanguageModelGateway:
    @property
    def model_name(self) -> str:
        return "demo"

    async def complete(self, request: ModelRequest) -> ModelReply:
        return ModelReply(
            text="主人说得有道理~",
            model=self.model_name,
            finish_reason="stop",
        )
