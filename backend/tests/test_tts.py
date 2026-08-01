import asyncio
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.tts import SpeakRequest, get_voices, speak
from core.config_loader import VoiceCatalog
from core.config_loader import get_voices as get_configured_voices
from core.config_loader import load_voice_catalog
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

    def test_explicit_default_voice_must_exist(self):
        with self.assertRaisesRegex(ValueError, "^unknown default voice$"):
            TTSService(
                voices=self.voices,
                default_voice="missing",
                fallback_voice="zh-CN-XiaoxiaoNeural",
                audio_dir=self.temp_dir.name,
            )

    def test_explicit_url_and_audio_age_override_environment(self):
        environment = {
            "ASSISTANT_GPT_SOVITS_URL": "https://env.example/tts",
            "ASSISTANT_AUDIO_MAX_AGE_SECONDS": "999",
        }
        with patch.dict(os.environ, environment, clear=False):
            service = TTSService(
                voices=self.voices,
                default_voice="character_001",
                fallback_voice="zh-CN-XiaoxiaoNeural",
                audio_dir=self.temp_dir.name,
                gpt_sovits_url="https://explicit.example/tts///",
                audio_max_age_seconds=0,
            )

        self.assertEqual(
            service.gpt_sovits_url,
            "https://explicit.example/tts",
        )
        self.assertEqual(service.audio_max_age_seconds, 0)

    def test_unset_audio_age_reads_environment(self):
        with patch.dict(
            os.environ,
            {"ASSISTANT_AUDIO_MAX_AGE_SECONDS": "321"},
            clear=False,
        ):
            service = TTSService(
                voices=self.voices,
                default_voice="character_001",
                fallback_voice="zh-CN-XiaoxiaoNeural",
                audio_dir=self.temp_dir.name,
            )

        self.assertEqual(service.audio_max_age_seconds, 321)

    def test_cleanup_honors_zero_and_negative_explicit_ages(self):
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            audio_max_age_seconds=999,
        )
        audio = Path(self.temp_dir.name) / "example.wav"

        for age in (0, -1):
            with self.subTest(age=age):
                audio.write_bytes(b"audio")
                os.utime(audio, (99, 99))
                with patch("core.tts.time.time", return_value=100):
                    removed = service.cleanup_expired_audio(age)
                self.assertEqual(removed, 1)

    def test_cleanup_skips_directories_and_non_audio_files(self):
        audio_directory = Path(self.temp_dir.name) / "unexpected.wav"
        audio_directory.mkdir()
        os.utime(audio_directory, (99, 99))
        non_audio = Path(self.temp_dir.name) / "notes.txt"
        non_audio.write_text("keep", encoding="utf-8")
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            audio_max_age_seconds=999,
        )
        expired = Path(self.temp_dir.name) / "expired.mp3"
        expired.write_bytes(b"audio")
        os.utime(expired, (99, 99))

        with patch("core.tts.time.time", return_value=100):
            removed = service.cleanup_expired_audio(0)

        self.assertEqual(removed, 1)
        self.assertTrue(audio_directory.is_dir())
        self.assertTrue(non_audio.is_file())

    def test_cleanup_continues_after_disappearance_and_os_errors(self):
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            audio_max_age_seconds=999,
        )
        vanished = Mock(suffix=".wav")
        vanished.is_file.side_effect = FileNotFoundError
        stat_error = Mock(suffix=".wav")
        stat_error.is_file.return_value = True
        stat_error.stat.side_effect = OSError("stat failed")
        unlink_error = Mock(suffix=".mp3")
        unlink_error.is_file.return_value = True
        unlink_error.stat.return_value.st_mtime = 0
        unlink_error.unlink.side_effect = OSError("unlink failed")
        service.audio_dir = Mock()
        service.audio_dir.iterdir.return_value = (
            vanished,
            stat_error,
            unlink_error,
        )

        with patch("core.tts.time.time", return_value=100):
            removed = service.cleanup_expired_audio(0)

        self.assertEqual(removed, 0)
        stat_error.unlink.assert_not_called()
        unlink_error.unlink.assert_called_once_with()

    def test_cleanup_returns_zero_when_directory_cannot_be_listed(self):
        service = TTSService(
            voices=self.voices,
            default_voice="character_001",
            fallback_voice="zh-CN-XiaoxiaoNeural",
            audio_dir=self.temp_dir.name,
            audio_max_age_seconds=999,
        )
        service.audio_dir = Mock()
        service.audio_dir.iterdir.side_effect = OSError("list failed")

        self.assertEqual(service.cleanup_expired_audio(0), 0)


class VoiceCatalogTests(unittest.TestCase):
    def _write_catalog(self, directory: str, content: str) -> Path:
        path = Path(directory) / "voices.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_valid_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_catalog(
                directory,
                """
voices:
  - id: character_002
    name: 小雪
    description: 温柔声线
    defaultParams:
      temperature: 1.0
default:
  voiceId: character_002
  fallbackVoice: zh-CN-XiaoxiaoNeural
""",
            )

            catalog = load_voice_catalog(path)

        self.assertEqual(catalog.voices[0]["id"], "character_002")
        self.assertEqual(catalog.default_voice, "character_002")
        self.assertEqual(catalog.fallback_voice, "zh-CN-XiaoxiaoNeural")

    def test_catalog_is_deeply_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_catalog(
                directory,
                """
voices:
  - id: character_001
    name: 小樱
    description: 活泼声线
    defaultParams:
      temperature: 1.0
default: {voiceId: character_001, fallbackVoice: fallback}
""",
            )
            catalog = load_voice_catalog(path)

        with self.assertRaises(TypeError):
            catalog.voices[0]["id"] = "mutated"
        with self.assertRaises(TypeError):
            catalog.voices[0]["defaultParams"]["temperature"] = 0.5

    def test_directly_constructed_catalog_is_deeply_immutable(self):
        catalog = VoiceCatalog(
            voices=(
                {
                    "id": "character_001",
                    "name": "小樱",
                    "description": "活泼声线",
                    "defaultParams": {"temperature": 1.0},
                },
            ),
            default_voice="character_001",
            fallback_voice="fallback",
        )

        with self.assertRaises(TypeError):
            catalog.voices[0]["id"] = "mutated"
        with self.assertRaises(TypeError):
            catalog.voices[0]["defaultParams"]["temperature"] = 0.5

    def test_compatibility_voice_lists_are_independent_deep_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_catalog(
                directory,
                """
voices:
  - id: character_001
    name: 小樱
    description: 活泼声线
    defaultParams:
      temperature: 1.0
default: {voiceId: character_001, fallbackVoice: fallback}
""",
            )
            with patch("core.config_loader._config_dir", Path(directory)):
                first = get_configured_voices()
                second = get_configured_voices()

        first[0]["id"] = "mutated"
        first[0]["defaultParams"]["temperature"] = 0.5
        self.assertEqual(second[0]["id"], "character_001")
        self.assertEqual(second[0]["defaultParams"]["temperature"], 1.0)

    def test_rejects_duplicate_yaml_mapping_keys_without_leaking_content(self):
        secret = "PRIVATE-DUPLICATE-KEY-CONTENT"
        cases = (
            f"""
voices:
  - id: {secret}
    id: character_001
    name: A
    description: first
default: {{voiceId: character_001, fallbackVoice: fallback}}
""",
            f"""
voices:
  - {{id: character_001, name: A, description: first}}
default:
  voiceId: {secret}
  voiceId: character_001
  fallbackVoice: fallback
""",
            f"""
voices: {secret}
voices:
  - {{id: character_001, name: A, description: first}}
default: {{voiceId: character_001, fallbackVoice: fallback}}
""",
        )
        for content in cases:
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = self._write_catalog(directory, content)
                with self.assertRaises(ValueError) as raised:
                    load_voice_catalog(path)

                error = raised.exception
                rendered = "".join(
                    traceback.format_exception(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                )
                self.assertEqual(str(error), "invalid voice catalog")
                self.assertIsNone(error.__cause__)
                self.assertNotIn(secret, rendered)
                context = error.__context__
                while context is not None:
                    self.assertNotIn(secret, str(context))
                    context = context.__context__

    def test_rejects_duplicate_ids_and_missing_default(self):
        cases = (
            """
voices:
  - {id: duplicate, name: A, description: first}
  - {id: duplicate, name: B, description: second}
default: {voiceId: duplicate, fallbackVoice: fallback}
""",
            """
voices:
  - {id: available, name: A, description: first}
default: {voiceId: missing, fallbackVoice: fallback}
""",
        )
        for content in cases:
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = self._write_catalog(directory, content)
                with self.assertRaisesRegex(
                    ValueError,
                    "^invalid voice catalog$",
                ):
                    load_voice_catalog(path)

    def test_rejects_wrong_field_types_without_leaking_content(self):
        secret = "PRIVATE-CATALOG-CONTENT"
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_catalog(
                directory,
                f"""
voices:
  - id: character_001
    name: 123
    description: {secret}
default: {{voiceId: character_001, fallbackVoice: fallback}}
""",
            )

            with self.assertRaises(ValueError) as raised:
                load_voice_catalog(path)

        self.assertEqual(str(raised.exception), "invalid voice catalog")
        self.assertNotIn(secret, str(raised.exception))

    def test_api_returns_503_when_all_providers_fail(self):
        runtime = Mock()
        runtime.application.tts.synthesize = AsyncMock(return_value=None)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(speak(SpeakRequest(text="测试"), runtime))

        self.assertEqual(raised.exception.status_code, 503)

    def test_tts_endpoints_use_the_runtime_application_service(self):
        runtime = Mock()
        runtime.application.tts.synthesize = AsyncMock(
            return_value={
                "audio_url": "/api/tts/audio/example.wav",
                "text": "测试",
            }
        )
        runtime.application.tts.get_voice_list.return_value = [
            {"id": "character_001", "name": "小樱"}
        ]

        spoken = asyncio.run(
            speak(
                SpeakRequest(text="  测试  ", voice_id="character_001"),
                runtime,
            )
        )
        voices = get_voices(runtime)

        self.assertEqual(spoken["text"], "测试")
        runtime.application.tts.synthesize.assert_awaited_once_with(
            "测试",
            "character_001",
        )
        self.assertEqual(voices, [{"id": "character_001", "name": "小樱"}])
