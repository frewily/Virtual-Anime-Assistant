import logging
import os
import time
import uuid
from pathlib import Path

import edge_tts
import httpx

from core.config_loader import get_default_voice, get_fallback_voice, get_voices

logger = logging.getLogger(__name__)

AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio"
DEFAULT_GPT_SOVITS_URL = "http://127.0.0.1:9880"
DEFAULT_AUDIO_MAX_AGE_SECONDS = 24 * 60 * 60


class TTSService:
    def __init__(
        self,
        *,
        voices: list[dict] | None = None,
        default_voice: str | None = None,
        fallback_voice: str | None = None,
        audio_dir: str | Path = AUDIO_DIR,
        gpt_sovits_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.voices = voices if voices is not None else get_voices()
        self._voices_by_id = {voice["id"]: voice for voice in self.voices}
        self.default_voice = default_voice or get_default_voice()
        self.fallback_voice = fallback_voice or get_fallback_voice()
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.gpt_sovits_url = (
            gpt_sovits_url
            or os.getenv("ASSISTANT_GPT_SOVITS_URL")
            or DEFAULT_GPT_SOVITS_URL
        ).rstrip("/")
        self._transport = transport
        self.cleanup_expired_audio()

    async def synthesize(self, text: str, voice_id: str | None = None) -> dict | None:
        self.cleanup_expired_audio()
        selected_voice = voice_id or self.default_voice
        if selected_voice not in self._voices_by_id:
            raise ValueError(f"unknown voice: {selected_voice}")

        result = await self._try_gpt_sovits(text, selected_voice)
        if result:
            return result
        return await self._try_edgetts(text)

    async def _try_gpt_sovits(self, text: str, voice_id: str) -> dict | None:
        voice = self._voices_by_id[voice_id]
        reference_audio = voice.get("referenceAudio")
        if not reference_audio:
            logger.warning("GPT-SoVITS skipped: voice %s has no reference audio", voice_id)
            return None

        request_body = {
            "text": text,
            "text_lang": "zh",
            "ref_audio_path": reference_audio,
            "prompt_text": voice.get("promptText", ""),
            "prompt_lang": "zh",
        }
        try:
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(f"{self.gpt_sovits_url}/tts", json=request_body)
            if response.status_code != 200 or not response.content:
                logger.warning(
                    "GPT-SoVITS failed for voice %s with status %s",
                    voice_id,
                    response.status_code,
                )
                return None

            filename = f"{uuid.uuid4().hex}.wav"
            (self.audio_dir / filename).write_bytes(response.content)
            return {"audio_url": f"/api/tts/audio/{filename}", "text": text}
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("GPT-SoVITS unavailable for voice %s: %s", voice_id, type(exc).__name__)
            return None

    async def _try_edgetts(self, text: str) -> dict | None:
        filename = f"{uuid.uuid4().hex}.mp3"
        filepath = self.audio_dir / filename
        try:
            await edge_tts.Communicate(text, self.fallback_voice).save(str(filepath))
            return {"audio_url": f"/api/tts/audio/{filename}", "text": text}
        except Exception as exc:
            logger.warning("EdgeTTS unavailable: %s", type(exc).__name__)
            filepath.unlink(missing_ok=True)
            return None

    def cleanup_expired_audio(self, max_age_seconds: int | None = None) -> int:
        max_age = max_age_seconds or int(
            os.getenv("ASSISTANT_AUDIO_MAX_AGE_SECONDS", DEFAULT_AUDIO_MAX_AGE_SECONDS)
        )
        cutoff = time.time() - max_age
        removed = 0
        for filepath in self.audio_dir.iterdir():
            if filepath.suffix.lower() not in {".mp3", ".wav"}:
                continue
            try:
                if filepath.stat().st_mtime < cutoff:
                    filepath.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def get_voice_list(self) -> list[dict]:
        return [
            {
                "id": voice["id"],
                "name": voice["name"],
                "description": voice["description"],
            }
            for voice in self.voices
        ]
