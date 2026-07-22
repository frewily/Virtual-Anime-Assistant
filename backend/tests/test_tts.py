import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.tts import SpeakRequest, speak
from core.tts import TTSService


class TTSServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.voices = [
            {
                "id": "character_001",
                "name": "小樱",
                "description": "测试声线",
                "referenceAudio": "voices/sakura.wav",
                "promptText": "你好",
            }
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_gpt_sovits_request_uses_configured_voice(self):
        captured = {}

        def handler(request: httpx.Request):
            captured["body"] = request.content.decode()
            return httpx.Response(200, content=b"RIFF-audio")

        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            transport=httpx.MockTransport(handler),
        )

        result = asyncio.run(service.synthesize("测试"))

        self.assertIn('"ref_audio_path":"voices/sakura.wav"', captured["body"])
        self.assertIn('"prompt_text":"你好"', captured["body"])
        self.assertTrue(result["audio_url"].endswith(".wav"))

    def test_gpt_failure_falls_back_to_edge_tts(self):
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        service._try_edgetts = AsyncMock(
            return_value={"audio_url": "/api/tts/audio/fallback.mp3", "text": "测试"}
        )

        result = asyncio.run(service.synthesize("测试"))

        self.assertEqual(result["audio_url"], "/api/tts/audio/fallback.mp3")
        service._try_edgetts.assert_awaited_once_with("测试")

    def test_unknown_voice_is_rejected(self):
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
        )

        with self.assertRaisesRegex(ValueError, "unknown voice"):
            asyncio.run(service.synthesize("测试", "missing"))

    def test_api_returns_503_when_all_providers_fail(self):
        with patch("api.tts.service.synthesize", AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(speak(SpeakRequest(text="测试")))

        self.assertEqual(raised.exception.status_code, 503)
