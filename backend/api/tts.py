from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.dependencies import get_runtime
from core.runtime import AssistantRuntime

router = APIRouter(tags=["tts"])


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    voice_id: str | None = None

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value.strip()


@router.post("/tts/speak")
async def speak(
    req: SpeakRequest,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    try:
        result = await runtime.application.tts.synthesize(
            req.text,
            req.voice_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=503, detail="all TTS providers failed")
    return result


@router.get("/tts/voices")
def get_voices(
    runtime: AssistantRuntime = Depends(get_runtime),
):
    return runtime.application.tts.get_voice_list()
