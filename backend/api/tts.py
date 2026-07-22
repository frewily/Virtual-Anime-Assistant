from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from core.tts import TTSService

router = APIRouter(tags=["tts"])
service = TTSService()


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
async def speak(req: SpeakRequest):
    try:
        result = await service.synthesize(req.text, req.voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=503, detail="all TTS providers failed")
    return result


@router.get("/tts/voices")
def get_voices():
    return service.get_voice_list()
