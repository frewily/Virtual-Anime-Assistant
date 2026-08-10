"""Tests for shared settings draft validation and connection probes."""

import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_loader import VoiceCatalog
from settings.models import SecretMutation
from settings.validation import (
    ConnectionTestCode,
    LLMSettingsDraft,
    LLMTestRequest,
    QQRuntimeStatus,
    QQSettingsDraft,
    QQTestRequest,
    SettingsDraft,
    SettingsValidationError,
    SettingsValidationService,
    TTSSettingsDraft,
    TTSTestRequest,
)
from llm.errors import ModelServiceError


class UnreadBodyStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterated = False
        self.closed = False

    async def __aiter__(self):
        self.iterated = True
        yield b"x" * (4 * 1024 * 1024)

    async def aclose(self) -> None:
        self.closed = True


class CloseTrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler) -> None:
        self._handler = handler
        self.close_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)

    async def aclose(self) -> None:
        self.close_count += 1


class ReadFailureStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request
        self.closed = False

    async def __aiter__(self):
        raise httpx.ReadError(
            "private streamed response content",
            request=self._request,
        )
        yield b""

    async def aclose(self) -> None:
        self.closed = True


class BlockingCloseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_completed = False

    async def __aiter__(self):
        yield b"unused"

    async def aclose(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.close_completed = True


class BlockingCloseTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: BlockingCloseStream) -> None:
        self._stream = stream
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_completed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, stream=self._stream)

    async def aclose(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.close_completed = True


class RequestAndCloseFailureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private request error", request=request)

    async def aclose(self) -> None:
        raise RuntimeError("private close error")


def voice_catalog() -> VoiceCatalog:
    return VoiceCatalog(
        voices=(
            {
                "id": "character_001",
                "name": "小樱",
                "description": "测试音色",
            },
        ),
        default_voice="character_001",
        fallback_voice="fallback",
    )


def valid_draft(**updates: object) -> SettingsDraft:
    values: dict[str, object] = {
        "llm": LLMSettingsDraft(),
        "qq": QQSettingsDraft(),
        "tts": TTSSettingsDraft(),
    }
    values.update(updates)
    return SettingsDraft(**values)


class SettingsValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SettingsValidationService(voice_catalog())

    def assert_field_errors(
        self,
        draft: SettingsDraft,
        expected: set[str],
        existing_secrets: dict[str, object] | None = None,
    ) -> SettingsValidationError:
        with self.assertRaises(SettingsValidationError) as raised:
            self.service.validate(draft, existing_secrets or {})
        self.assertEqual(set(raised.exception.fields), expected)
        return raised.exception

    def test_llm_enabled_requires_url_model_and_effective_api_key(self) -> None:
        draft = valid_draft(llm=LLMSettingsDraft(enabled=True))

        self.assert_field_errors(
            draft,
            {"llm.baseUrl", "llm.model", "llm.apiKey"},
        )

    def test_retained_existing_llm_secret_counts_as_configured(self) -> None:
        draft = valid_draft(
            llm=LLMSettingsDraft(
                enabled=True,
                base_url="https://example.test/v1",
                model="tiny-model",
            )
        )

        validated = self.service.validate(
            draft,
            {"llm.apiKey": "stored-test-key"},
        )

        self.assertTrue(validated.secret_configured["llm.apiKey"])
        self.assertNotIn("stored-test-key", repr(validated))

    def test_validated_draft_is_deeply_immutable(self) -> None:
        draft = valid_draft(
            llm=LLMSettingsDraft(
                api_key=SecretMutation(operation="replace", value="original-key")
            ),
            qq=QQSettingsDraft(allowed_group_ids=[10001]),
        )

        validated = self.service.validate(draft, {})
        draft.qq.allowed_group_ids.append(20002)

        self.assertEqual(validated.draft.qq.allowed_group_ids, (10001,))
        with self.assertRaises((AttributeError, TypeError, ValueError)):
            validated.draft.qq.enabled = True
        with self.assertRaises(AttributeError):
            validated.draft.qq.allowed_group_ids.append(-1)
        with self.assertRaises(TypeError):
            validated.draft.qq.model_copy(update={"allowed_group_ids": (-1,)})
        with self.assertRaises(TypeError):
            validated.draft.model_copy(
                update={"tts": {"default_voice_id": "missing"}}
            )
        with self.assertRaises(TypeError):
            validated.secret_configured["llm.apiKey"] = False

        exposed_secret = validated.effective_secret("llm.apiKey")
        self.assertIsNotNone(exposed_secret)
        object.__setattr__(exposed_secret, "_secret_value", "changed-key")

        self.assertFalse(validated.draft.qq.enabled)
        self.assertEqual(validated.draft.qq.allowed_group_ids, (10001,))
        self.assertEqual(validated.draft.tts.default_voice_id, "character_001")
        self.assertEqual(
            validated.effective_secret("llm.apiKey").get_secret_value(),
            "original-key",
        )
        serialized = validated.draft.model_dump_json(by_alias=True)
        self.assertNotIn("original-key", serialized)
        self.assertIn('"allowedGroupIds":[10001]', serialized)
        for rendered in (
            repr(validated._secret_values),
            repr(validated),
            repr(validated.draft),
            str(validated.draft.model_dump(mode="json", by_alias=True)),
            validated.draft.model_dump_json(by_alias=True),
        ):
            self.assertNotIn("original-key", rendered)
        with self.assertRaises(TypeError) as raised:
            validated.draft.model_copy(
                update={"llm": {"apiKey": "original-key"}}
            )
        self.assertNotIn("original-key", str(raised.exception))

    def test_delete_and_blank_replacement_are_not_effective_secrets(self) -> None:
        for mutation in (
            SecretMutation(operation="delete"),
            SecretMutation(operation="replace", value="   "),
        ):
            with self.subTest(operation=mutation.operation):
                draft = valid_draft(
                    llm=LLMSettingsDraft(
                        enabled=True,
                        base_url="https://example.test/v1",
                        model="tiny-model",
                        api_key=mutation,
                    )
                )
                self.assert_field_errors(
                    draft,
                    {"llm.apiKey"},
                    {"llm.apiKey": "stored-test-key"},
                )

    def test_url_rejects_userinfo_and_reports_stable_field(self) -> None:
        draft = valid_draft(
            llm=LLMSettingsDraft(
                enabled=True,
                base_url="https://user:pass@example.test/v1?secret=query",
                model="tiny-model",
                api_key=SecretMutation(operation="replace", value="test-key"),
            )
        )

        error = self.assert_field_errors(draft, {"llm.baseUrl"})

        rendered = f"{error!s}{error!r}{error.to_dict()}{error.json()}"
        self.assertNotIn("user", rendered)
        self.assertNotIn("pass", rendered)
        self.assertNotIn("query", rendered)
        self.assertNotIn("test-key", rendered)

    def test_urls_only_allow_http_and_https(self) -> None:
        for url, field in (
            ("file:///tmp/model", "llm.baseUrl"),
            ("ws://127.0.0.1:9880", "tts.gptSovitsUrl"),
            ("https:///missing-host", "llm.baseUrl"),
        ):
            with self.subTest(url=url):
                if field.startswith("llm"):
                    draft = valid_draft(
                        llm=LLMSettingsDraft(
                            enabled=True,
                            base_url=url,
                            model="model",
                            api_key=SecretMutation(
                                operation="replace", value="test-key"
                            ),
                        )
                    )
                else:
                    draft = valid_draft(
                        tts=TTSSettingsDraft(gpt_sovits_url=url)
                    )
                self.assert_field_errors(draft, {field})

    def test_urls_reject_query_fragment_and_all_control_characters(self) -> None:
        cases = (
            (
                valid_draft(
                    llm=LLMSettingsDraft(base_url="https://example.test/v1?q=x")
                ),
                "llm.baseUrl",
            ),
            (
                valid_draft(
                    tts=TTSSettingsDraft(
                        gpt_sovits_url="https://example.test/#fragment"
                    )
                ),
                "tts.gptSovitsUrl",
            ),
            (
                valid_draft(
                    tts=TTSSettingsDraft(
                        gpt_sovits_url="https://example.test/\x7fpath"
                    )
                ),
                "tts.gptSovitsUrl",
            ),
            (
                valid_draft(
                    llm=LLMSettingsDraft(base_url="https://example.test/v1?")
                ),
                "llm.baseUrl",
            ),
            (
                valid_draft(
                    tts=TTSSettingsDraft(
                        gpt_sovits_url="https://example.test/#"
                    )
                ),
                "tts.gptSovitsUrl",
            ),
        )
        for draft, field in cases:
            with self.subTest(field=field):
                self.assert_field_errors(draft, {field})

    def test_qq_enabled_requires_token_and_an_allowlist(self) -> None:
        draft = valid_draft(qq=QQSettingsDraft(enabled=True))

        self.assert_field_errors(
            draft,
            {"qq.accessToken", "qq.allowedGroupIds"},
        )

    def test_qq_token_length_and_rate_relationship_are_validated(self) -> None:
        draft = valid_draft(
            qq=QQSettingsDraft(
                enabled=True,
                allowed_group_ids=[10001],
                access_token=SecretMutation(operation="replace", value="too-short"),
                rate_per_minute=5,
                rate_burst=6,
            )
        )

        self.assert_field_errors(
            draft,
            {"qq.accessToken", "qq.rateBurst"},
        )

    def test_disabled_qq_still_validates_replacement_token_length(self) -> None:
        for token in ("too-short", "x" * 513):
            with self.subTest(length=len(token)):
                draft = valid_draft(
                    qq=QQSettingsDraft(
                        enabled=False,
                        access_token=SecretMutation(
                            operation="replace",
                            value=token,
                        ),
                    )
                )

                error = self.assert_field_errors(draft, {"qq.accessToken"})
                rendered = f"{error!s}{error!r}{error.to_dict()}{error.json()}"
                self.assertNotIn(token, rendered)

    def test_numeric_runtime_boundaries_are_enforced(self) -> None:
        cases = (
            (LLMSettingsDraft(timeout_seconds=0), "llm.timeoutSeconds", None),
            (
                LLMSettingsDraft(max_context_messages=101),
                "llm.maxContextMessages",
                None,
            ),
            (LLMSettingsDraft(max_context_chars=3999), "llm.maxContextChars", None),
            (None, "qq.ratePerMinute", QQSettingsDraft(rate_per_minute=121)),
            (None, "qq.rateBurst", QQSettingsDraft(rate_burst=21)),
            (None, "qq.maxConcurrency", QQSettingsDraft(max_concurrency=0)),
            (
                None,
                "qq.actionTimeoutSeconds",
                QQSettingsDraft(action_timeout_seconds=61),
            ),
        )
        for llm, field, qq in cases:
            with self.subTest(field=field):
                self.assert_field_errors(
                    valid_draft(
                        llm=llm or LLMSettingsDraft(),
                        qq=qq or QQSettingsDraft(),
                    ),
                    {field},
                )

    def test_tts_audio_age_accepts_any_integer(self) -> None:
        for value in (-1, 0, 10**9):
            with self.subTest(value=value):
                self.service.validate(
                    valid_draft(tts=TTSSettingsDraft(audio_max_age_seconds=value)),
                    {},
                )

    def test_tts_default_voice_must_exist(self) -> None:
        self.assert_field_errors(
            valid_draft(tts=TTSSettingsDraft(default_voice_id="missing")),
            {"tts.defaultVoiceId"},
        )

    def test_invalid_existing_secret_mapping_values_fail_closed(self) -> None:
        draft = valid_draft(
            llm=LLMSettingsDraft(
                enabled=True,
                base_url="https://example.test/v1",
                model="model",
            )
        )
        for value in (object(), 1, {"secret": "do-not-leak"}):
            with self.subTest(value=type(value).__name__):
                error = self.assert_field_errors(
                    draft,
                    {"llm.apiKey"},
                    {"llm.apiKey": value},
                )
                self.assertNotIn("do-not-leak", str(error.to_dict()))


class ConnectionProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = SettingsValidationService(voice_catalog())

    async def test_llm_uses_minimal_gateway_request_and_fixed_timeout(self) -> None:
        observed: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["url"] = str(request.url)
            observed["path"] = request.url.path
            observed["authorization"] = request.headers.get("authorization")
            observed["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "model": "tiny-model",
                },
            )

        result = await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="tiny-model",
                api_key="temporary-test-key",
            ),
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, ConnectionTestCode.SUCCESS)
        self.assertEqual(observed["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(observed["path"], "/v1/chat/completions")
        self.assertEqual(observed["authorization"], "Bearer temporary-test-key")
        payload = observed["payload"]
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(len(payload["messages"]), 1)
        self.assertNotIn("tools", payload)

    async def test_llm_accepts_reasoning_only_probe_response(self) -> None:
        result = await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="thinking-model",
                api_key="temporary-test-key",
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "reasoning_content": "The service is responding.",
                                }
                            }
                        ],
                        "model": "thinking-model",
                    },
                )
            ),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, ConnectionTestCode.SUCCESS)

    async def test_llm_401_is_redacted_authentication_failure(self) -> None:
        test_key = "VERY-PRIVATE-TEST-KEY"
        result = await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="tiny-model",
                api_key=test_key,
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401,
                    text=f"invalid {test_key} at {request.url}",
                )
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, ConnectionTestCode.AUTHENTICATION_FAILED)
        serialized = result.model_dump_json(by_alias=True)
        self.assertNotIn(test_key, serialized)

    async def test_llm_transport_failures_are_stably_classified(self) -> None:
        async def run(exception: Exception):
            def handler(_: httpx.Request) -> httpx.Response:
                raise exception

            return await self.service.test_llm(
                LLMTestRequest(
                    base_url="https://example.test/v1",
                    model="model",
                    api_key="test-key",
                ),
                transport=httpx.MockTransport(handler),
            )

        request = httpx.Request("POST", "https://example.test")
        timeout = await run(httpx.ReadTimeout("private timeout", request=request))
        unreachable = await run(httpx.ConnectError("private host", request=request))
        untrusted = await run(RuntimeError("private response body"))

        self.assertEqual(timeout.code, ConnectionTestCode.TIMED_OUT)
        self.assertEqual(unreachable.code, ConnectionTestCode.UNREACHABLE)
        self.assertEqual(untrusted.code, ConnectionTestCode.SERVICE_ERROR)
        serialized = (
            timeout.model_dump_json()
            + unreachable.model_dump_json()
            + untrusted.model_dump_json()
        )
        self.assertNotIn("private", serialized)

    async def test_llm_default_transport_ignores_invalid_ca_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"SSL_CERT_FILE": "/definitely/not/a/real/ca.pem"},
        ):
            result = await self.service.test_llm(
                LLMTestRequest(
                    base_url="http://127.0.0.1:1/v1",
                    model="model",
                    api_key="test-key",
                )
            )

        self.assertEqual(result.code, ConnectionTestCode.UNREACHABLE)

    async def test_llm_stream_read_failure_is_unreachable_and_closes(self) -> None:
        streams: list[ReadFailureStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            stream = ReadFailureStream(request)
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        transport = CloseTrackingTransport(handler)
        result = await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="model",
                api_key="test-key",
            ),
            transport=transport,
        )

        self.assertEqual(result.code, ConnectionTestCode.UNREACHABLE)
        self.assertEqual(transport.close_count, 1)
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].closed)
        self.assertNotIn("private", result.model_dump_json())

    async def test_llm_http_service_error_is_not_reported_as_unreachable(self) -> None:
        result = await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="model",
                api_key="test-key",
            ),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(503, text="private upstream body")
            ),
        )

        self.assertEqual(result.code, ConnectionTestCode.SERVICE_ERROR)
        self.assertNotIn("private", result.model_dump_json())

    async def test_llm_service_classification_does_not_parse_error_text(self) -> None:
        with patch(
            "settings.validation.OpenAICompatibleGateway.complete",
            new=AsyncMock(side_effect=ModelServiceError("private localized text")),
        ):
            result = await self.service.test_llm(
                LLMTestRequest(
                    base_url="https://example.test/v1",
                    model="model",
                    api_key="test-key",
                )
            )

        self.assertEqual(result.code, ConnectionTestCode.SERVICE_ERROR)
        self.assertNotIn("private", result.model_dump_json())

    async def test_tts_falls_back_from_openapi_404_to_root_204(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(404 if request.url.path == "/openapi.json" else 204)

        result = await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.ok)
        self.assertEqual(paths, ["/openapi.json", "/"])

    async def test_tts_success_does_not_request_root_or_generate_audio(self) -> None:
        methods_and_paths: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods_and_paths.append((request.method, request.url.path))
            return httpx.Response(302)

        result = await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.ok)
        self.assertEqual(methods_and_paths, [("GET", "/openapi.json")])

    async def test_tts_never_reads_probe_response_bodies(self) -> None:
        streams: list[UnreadBodyStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            stream = UnreadBodyStream()
            streams.append(stream)
            status = 404 if request.url.path == "/openapi.json" else 204
            return httpx.Response(status, stream=stream)

        result = await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
            transport=httpx.MockTransport(handler),
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertFalse(any(stream.iterated for stream in streams))

    async def test_tts_cancellation_waits_for_all_close_layers(self) -> None:
        stream = BlockingCloseStream()
        transport = BlockingCloseTransport(stream)
        task = asyncio.create_task(
            self.service.test_tts(
                TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
                transport=transport,
            )
        )

        await asyncio.wait_for(stream.close_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        stream.allow_close.set()
        await asyncio.wait_for(transport.close_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        transport.allow_close.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(stream.close_completed)
        self.assertTrue(transport.close_completed)

    async def test_tts_cleanup_timeout_is_bounded(self) -> None:
        stream = BlockingCloseStream()
        transport = CloseTrackingTransport(
            lambda _: httpx.Response(204, stream=stream)
        )
        loop = asyncio.get_running_loop()
        started = loop.time()

        with (
            patch("settings.validation._CLEANUP_TIMEOUT_SECONDS", 0.02),
            patch("settings.validation._CLEANUP_CANCEL_GRACE_SECONDS", 0.01),
        ):
            result = await self.service.test_tts(
                TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
                transport=transport,
            )

        elapsed = loop.time() - started
        self.assertEqual(result.code, ConnectionTestCode.TIMED_OUT)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(transport.close_count, 1)

    async def test_tts_transport_errors_are_redacted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "private-service-name?token=secret",
                request=request,
            )

        result = await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="https://example.test"),
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result.code, ConnectionTestCode.UNREACHABLE)
        self.assertNotIn("secret", result.model_dump_json())

    async def test_tts_close_error_does_not_replace_request_error(self) -> None:
        result = await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="https://example.test"),
            transport=RequestAndCloseFailureTransport(),
        )

        self.assertEqual(result.code, ConnectionTestCode.UNREACHABLE)
        self.assertNotIn("private", result.model_dump_json())

    async def test_probe_clients_close_injected_transports(self) -> None:
        llm_transport = CloseTrackingTransport(
            lambda _: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        )
        tts_transport = CloseTrackingTransport(lambda _: httpx.Response(204))

        await self.service.test_llm(
            LLMTestRequest(
                base_url="https://example.test/v1",
                model="model",
                api_key="test-key",
            ),
            transport=llm_transport,
        )
        await self.service.test_tts(
            TTSTestRequest(gpt_sovits_url="https://example.test"),
            transport=tts_transport,
        )

        self.assertEqual(llm_transport.close_count, 1)
        self.assertEqual(tts_transport.close_count, 1)

    async def test_qq_only_returns_injected_current_runtime_status(self) -> None:
        request = QQTestRequest(
            enabled=True,
            allowed_group_ids=[10001],
            access_token="1234567890abcdef",
            rate_per_minute=10,
            rate_burst=2,
            max_concurrency=4,
            action_timeout_seconds=10,
        )
        status = QQRuntimeStatus(
            enabled=True,
            state="connected",
            allowed_group_count=1,
            allowed_user_count=0,
        )

        result = await self.service.test_qq(request, status)

        self.assertTrue(result.ok)
        self.assertTrue(result.current_runtime_config)
        self.assertEqual(result.status, status)
        self.assertNotIn("1234567890abcdef", result.model_dump_json())

    async def test_qq_invalid_status_mapping_fails_closed_without_secret_leak(
        self,
    ) -> None:
        result = await self.service.test_qq(
            QQTestRequest(enabled=False),
            {"state": "private-status", "token": "private-token"},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, ConnectionTestCode.SERVICE_ERROR)
        self.assertNotIn("private", result.model_dump_json())

    async def test_qq_revalidates_forged_runtime_status_instances(self) -> None:
        valid = QQRuntimeStatus(
            enabled=True,
            state="connected",
            allowed_group_count=1,
            allowed_user_count=0,
        )
        forged_statuses = (
            QQRuntimeStatus.model_construct(
                enabled=True,
                state="private-constructed-state",
                allowed_group_count=-1,
                allowed_user_count=0,
            ),
            valid.model_copy(
                update={
                    "state": "private-copied-state",
                    "allowed_group_count": -1,
                }
            ),
        )

        for status in forged_statuses:
            with self.subTest(state=status.state):
                result = await self.service.test_qq(
                    QQTestRequest(enabled=False),
                    status,
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.code, ConnectionTestCode.SERVICE_ERROR)
                self.assertNotIn("private", result.model_dump_json())

    async def test_cancelled_probes_are_not_swallowed(self) -> None:
        async def blocking(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(60)
            return httpx.Response(200)

        task = asyncio.create_task(
            self.service.test_tts(
                TTSTestRequest(gpt_sovits_url="http://127.0.0.1:9880"),
                transport=httpx.MockTransport(blocking),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
