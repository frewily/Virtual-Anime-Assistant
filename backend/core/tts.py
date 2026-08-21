from copy import deepcopy
import logging
import os
import stat
import time
import uuid
from pathlib import Path

import edge_tts
import httpx

from core.config_loader import load_voice_catalog

logger = logging.getLogger(__name__)

_configured_audio_dir = os.getenv("ASSISTANT_AUDIO_DIR", "").strip()
AUDIO_DIR = (
    Path(_configured_audio_dir).expanduser()
    if _configured_audio_dir
    else Path(__file__).resolve().parent.parent / "audio"
)
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
        audio_max_age_seconds: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        catalog = None
        if voices is None or default_voice is None or fallback_voice is None:
            catalog = load_voice_catalog()
        self.voices = (
            deepcopy(voices)
            if voices is not None
            else catalog.copy_voices()
        )
        self._voices_by_id = {voice["id"]: voice for voice in self.voices}
        self.default_voice = (
            default_voice
            if default_voice is not None
            else catalog.default_voice
        )
        if default_voice is not None and default_voice not in self._voices_by_id:
            raise ValueError("unknown default voice")
        self.fallback_voice = (
            fallback_voice
            if fallback_voice is not None
            else catalog.fallback_voice
        )
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.gpt_sovits_url = (
            gpt_sovits_url
            if gpt_sovits_url is not None
            else os.getenv("ASSISTANT_GPT_SOVITS_URL")
            or DEFAULT_GPT_SOVITS_URL
        ).rstrip("/")
        self.audio_max_age_seconds = (
            audio_max_age_seconds
            if audio_max_age_seconds is not None
            else int(
                os.getenv(
                    "ASSISTANT_AUDIO_MAX_AGE_SECONDS",
                    DEFAULT_AUDIO_MAX_AGE_SECONDS,
                )
            )
        )
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
        max_age = (
            max_age_seconds
            if max_age_seconds is not None
            else self.audio_max_age_seconds
        )
        cutoff = time.time() - max_age
        removed = 0
        try:
            entries = list(self.audio_dir.iterdir())
        except OSError:
            logger.warning("Audio cleanup skipped: directory unavailable")
            return 0
        for filepath in entries:
            try:
                if filepath.suffix.lower() not in {".mp3", ".wav"}:
                    continue
                metadata = filepath.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if metadata.st_mtime < cutoff:
                    filepath.unlink()
                    removed += 1
            except OSError:
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
