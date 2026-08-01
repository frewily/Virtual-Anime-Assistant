"""Shared validation and redacted connection probes for settings drafts."""

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import sys
from types import MappingProxyType
from typing import Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import ConfigDict, Field, SecretStr, ValidationError

from core.config_loader import VoiceCatalog
from llm.config import LLMSettings as RuntimeLLMSettings
from llm.errors import (
    ModelAuthenticationError,
    ModelConfigurationError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServiceError,
    ModelTimeoutError,
)
from llm.models import ModelMessage, ModelRequest, ModelRole
from llm.openai_compatible import OpenAICompatibleGateway
from settings.models import RequestModel, SecretMutation, SecretOperation


class _StrictRequestModel(RequestModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=RequestModel.model_config["alias_generator"],
        hide_input_in_errors=True,
        serialize_by_alias=True,
        strict=True,
        validate_assignment=True,
    )


class LLMSettingsDraft(_StrictRequestModel):
    enabled: bool = False
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = 60
    max_context_messages: int = 20
    max_context_chars: int = 12000
    tool_calling_enabled: bool = False
    api_key: SecretMutation = Field(default_factory=SecretMutation)


class QQSettingsDraft(_StrictRequestModel):
    enabled: bool = False
    allowed_group_ids: list[int] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    rate_per_minute: int = 10
    rate_burst: int = 2
    max_concurrency: int = 4
    action_timeout_seconds: int = 10
    access_token: SecretMutation = Field(default_factory=SecretMutation)


class TTSSettingsDraft(_StrictRequestModel):
    gpt_sovits_url: str = "http://127.0.0.1:9880"
    default_voice_id: str = "character_001"
    audio_max_age_seconds: int = 86400


class SettingsDraft(_StrictRequestModel):
    llm: LLMSettingsDraft = Field(default_factory=LLMSettingsDraft)
    qq: QQSettingsDraft = Field(default_factory=QQSettingsDraft)
    tts: TTSSettingsDraft = Field(default_factory=TTSSettingsDraft)


class _ValidatedModel(_StrictRequestModel):
    model_config = ConfigDict(
        **_StrictRequestModel.model_config,
        frozen=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("validated settings snapshots cannot be updated")
        return super().model_copy(deep=True)


class ValidatedSecretMutation(_ValidatedModel):
    operation: SecretOperation


class ValidatedLLMSettings(_ValidatedModel):
    enabled: bool
    base_url: str | None
    model: str | None
    timeout_seconds: int
    max_context_messages: int
    max_context_chars: int
    tool_calling_enabled: bool
    api_key: ValidatedSecretMutation


class ValidatedQQSettings(_ValidatedModel):
    enabled: bool
    allowed_group_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    rate_per_minute: int
    rate_burst: int
    max_concurrency: int
    action_timeout_seconds: int
    access_token: ValidatedSecretMutation


class ValidatedTTSSettings(_ValidatedModel):
    gpt_sovits_url: str
    default_voice_id: str
    audio_max_age_seconds: int


class ValidatedSettingsSnapshot(_ValidatedModel):
    llm: ValidatedLLMSettings
    qq: ValidatedQQSettings
    tts: ValidatedTTSSettings


@dataclass(frozen=True, slots=True)
class ValidatedDraft:
    """Validated draft plus effective secret presence, without public plaintext."""

    draft: ValidatedSettingsSnapshot
    secret_configured: Mapping[str, bool]
    _secret_values: Mapping[str, SecretStr | None] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "secret_configured",
            MappingProxyType(dict(self.secret_configured)),
        )
        object.__setattr__(
            self,
            "_secret_values",
            MappingProxyType(dict(self._secret_values)),
        )

    def effective_secret(self, field_path: str) -> SecretStr | None:
        """Return an effective secret for an in-memory probe or save transaction."""

        value = self._secret_values.get(field_path)
        return (
            SecretStr(value.get_secret_value())
            if value is not None
            else None
        )


class SettingsValidationError(ValueError):
    """A stable, serializable field error that never contains input values."""

    code = "SETTINGS_VALIDATION_FAILED"

    def __init__(self, fields: Mapping[str, str]):
        safe_fields = {
            str(key): str(message)
            for key, message in fields.items()
            if isinstance(key, str) and isinstance(message, str)
        }
        self.fields = MappingProxyType(safe_fields)
        super().__init__("settings validation failed")

    def to_dict(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": "请检查标记的配置项",
                "fields": dict(self.fields),
            }
        }

    def json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def __repr__(self) -> str:
        return f"SettingsValidationError(fields={dict(self.fields)!r})"


class ConnectionTestCode(str, Enum):
    SUCCESS = "SUCCESS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMED_OUT = "TIMED_OUT"
    UNREACHABLE = "UNREACHABLE"
    INCOMPATIBLE_RESPONSE = "INCOMPATIBLE_RESPONSE"
    SERVICE_ERROR = "SERVICE_ERROR"


class ConnectionTestResult(_StrictRequestModel):
    ok: bool
    code: ConnectionTestCode


class LLMTestRequest(_StrictRequestModel):
    base_url: str
    model: str
    api_key: SecretStr | None = None


class QQTestRequest(_StrictRequestModel):
    enabled: bool = False
    allowed_group_ids: list[int] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    rate_per_minute: int = 10
    rate_burst: int = 2
    max_concurrency: int = 4
    action_timeout_seconds: int = 10
    access_token: SecretStr | None = None


class QQRuntimeStatus(_StrictRequestModel):
    enabled: bool
    state: Literal["disabled", "misconfigured", "disconnected", "connected"]
    allowed_group_count: int = Field(ge=0)
    allowed_user_count: int = Field(ge=0)


class QQConnectionTestResult(ConnectionTestResult):
    current_runtime_config: bool = True
    status: QQRuntimeStatus | None = None


class TTSTestRequest(_StrictRequestModel):
    gpt_sovits_url: str


class _NetworkClassifyingTransport(httpx.AsyncBaseTransport):
    """Track only a safe failure category hidden by the existing gateway."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner
        self.unreachable = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except httpx.TimeoutException:
            raise
        except (httpx.RequestError, OSError):
            self.unreachable = True
            raise
        response.stream = _ClassifyingResponseStream(response.stream, self)
        return response

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        except (httpx.RequestError, OSError):
            self.unreachable = True
            raise


class _ClassifyingResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        transport: _NetworkClassifyingTransport,
    ) -> None:
        self._inner = inner
        self._transport = transport

    async def __aiter__(self):
        try:
            async for chunk in self._inner:
                yield chunk
        except httpx.TimeoutException:
            raise
        except (httpx.RequestError, OSError):
            self._transport.unreachable = True
            raise

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        except httpx.TimeoutException:
            raise
        except (httpx.RequestError, OSError):
            self._transport.unreachable = True
            raise


_FIELD_MESSAGE = "配置值无效"
_REQUIRED_MESSAGE = "启用时必须配置此项"
_TTS_NETWORK_TIMEOUT_SECONDS = 8.5


class SettingsValidationService:
    """Validate complete drafts and run bounded, side-effect-free probes."""

    def __init__(self, voice_catalog: VoiceCatalog):
        self._voice_ids = frozenset(
            voice.get("id")
            for voice in voice_catalog.voices
            if isinstance(voice.get("id"), str)
        )

    def validate(
        self,
        draft: SettingsDraft,
        existing_secrets: Mapping[str, object],
    ) -> ValidatedDraft:
        errors: dict[str, str] = {}
        secret_values: dict[str, SecretStr | None] = {}
        secret_configured: dict[str, bool] = {}

        for path, mutation in (
            ("llm.apiKey", draft.llm.api_key),
            ("qq.accessToken", draft.qq.access_token),
        ):
            secret = self._effective_secret(
                mutation,
                existing_secrets.get(path),
            )
            secret_values[path] = secret
            secret_configured[path] = secret is not None or (
                mutation.operation is SecretOperation.RETAIN
                and existing_secrets.get(path) is True
            )

        llm = draft.llm
        if _nonempty(llm.base_url) and not _is_safe_http_url(llm.base_url):
            errors["llm.baseUrl"] = _FIELD_MESSAGE
        if llm.enabled:
            if not _nonempty(llm.base_url):
                errors["llm.baseUrl"] = _REQUIRED_MESSAGE
            if not _nonempty(llm.model):
                errors["llm.model"] = _REQUIRED_MESSAGE
            if not secret_configured["llm.apiKey"]:
                errors["llm.apiKey"] = _REQUIRED_MESSAGE
        _bounded(errors, "llm.timeoutSeconds", llm.timeout_seconds, 1, 300)
        _bounded(
            errors,
            "llm.maxContextMessages",
            llm.max_context_messages,
            1,
            100,
        )
        _bounded(
            errors,
            "llm.maxContextChars",
            llm.max_context_chars,
            4000,
            100000,
        )

        qq = draft.qq
        _positive_ids(errors, "qq.allowedGroupIds", qq.allowed_group_ids)
        _positive_ids(errors, "qq.allowedUserIds", qq.allowed_user_ids)
        _bounded(errors, "qq.ratePerMinute", qq.rate_per_minute, 1, 120)
        _bounded(errors, "qq.rateBurst", qq.rate_burst, 1, 20)
        _bounded(errors, "qq.maxConcurrency", qq.max_concurrency, 1, 32)
        _bounded(
            errors,
            "qq.actionTimeoutSeconds",
            qq.action_timeout_seconds,
            1,
            60,
        )
        if qq.rate_burst > qq.rate_per_minute:
            errors["qq.rateBurst"] = _FIELD_MESSAGE
        qq_secret = secret_values["qq.accessToken"]
        if (
            qq.access_token.operation is SecretOperation.REPLACE
            and not _valid_qq_token(qq_secret)
        ):
            errors["qq.accessToken"] = _FIELD_MESSAGE
        if qq.enabled:
            if not secret_configured["qq.accessToken"]:
                errors.setdefault("qq.accessToken", _REQUIRED_MESSAGE)
            elif (
                qq.access_token.operation is SecretOperation.RETAIN
                and qq_secret is not None
                and not _valid_qq_token(qq_secret)
            ):
                errors["qq.accessToken"] = _FIELD_MESSAGE
            if not qq.allowed_group_ids and not qq.allowed_user_ids:
                errors["qq.allowedGroupIds"] = _REQUIRED_MESSAGE

        tts = draft.tts
        if not _is_safe_http_url(tts.gpt_sovits_url):
            errors["tts.gptSovitsUrl"] = _FIELD_MESSAGE
        if tts.default_voice_id not in self._voice_ids:
            errors["tts.defaultVoiceId"] = _FIELD_MESSAGE

        if errors:
            raise SettingsValidationError(errors) from None
        return ValidatedDraft(
            draft=_validated_snapshot(draft),
            secret_configured=secret_configured,
            _secret_values={
                path: (
                    SecretStr(secret.get_secret_value())
                    if secret is not None
                    else None
                )
                for path, secret in secret_values.items()
            },
        )

    async def test_llm(
        self,
        request: LLMTestRequest,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        if (
            not _is_safe_http_url(request.base_url)
            or not _nonempty(request.model)
        ):
            return _result(ConnectionTestCode.VALIDATION_FAILED)

        api_key = (
            request.api_key.get_secret_value().strip()
            if request.api_key is not None
            else None
        )
        settings = RuntimeLLMSettings(
            enabled=True,
            base_url=request.base_url,
            api_key=api_key,
            model=request.model.strip(),
            timeout_seconds=15,
            max_context_messages=1,
            max_context_chars=4000,
            tool_calling_enabled=False,
        )
        classified_transport: _NetworkClassifyingTransport | None = None
        try:
            classified_transport = _NetworkClassifyingTransport(
                transport
                if transport is not None
                else httpx.AsyncHTTPTransport(trust_env=False)
            )
            gateway = OpenAICompatibleGateway(
                settings,
                transport=classified_transport,
            )
            await asyncio.wait_for(
                gateway.complete(
                    ModelRequest(
                        correlation_id="settings-connection-test",
                        messages=[
                            ModelMessage(role=ModelRole.USER, content="ping")
                        ],
                        tools=[],
                        max_output_tokens=1,
                    )
                ),
                timeout=15,
            )
        except ModelAuthenticationError:
            return _result(ConnectionTestCode.AUTHENTICATION_FAILED)
        except ModelRateLimitError:
            return _result(ConnectionTestCode.RATE_LIMITED)
        except (ModelTimeoutError, TimeoutError):
            return _result(ConnectionTestCode.TIMED_OUT)
        except ModelProtocolError:
            return _result(ConnectionTestCode.INCOMPATIBLE_RESPONSE)
        except ModelServiceError:
            code = (
                ConnectionTestCode.UNREACHABLE
                if classified_transport is not None
                and classified_transport.unreachable
                else ConnectionTestCode.SERVICE_ERROR
            )
            return _result(code)
        except (httpx.RequestError, OSError):
            return _result(ConnectionTestCode.UNREACHABLE)
        except ModelConfigurationError:
            return _result(ConnectionTestCode.VALIDATION_FAILED)
        except Exception:
            return _result(ConnectionTestCode.SERVICE_ERROR)
        return _result(ConnectionTestCode.SUCCESS)

    async def test_qq(
        self,
        request: QQTestRequest,
        current_status: QQRuntimeStatus | Mapping[str, object],
    ) -> QQConnectionTestResult:
        access_token = (
            request.access_token.get_secret_value()
            if request.access_token is not None
            else None
        )
        mutation = (
            SecretMutation(operation="replace", value=access_token)
            if access_token is not None
            else SecretMutation(operation="retain")
        )
        draft = SettingsDraft(
            qq=QQSettingsDraft(
                enabled=request.enabled,
                allowed_group_ids=request.allowed_group_ids,
                allowed_user_ids=request.allowed_user_ids,
                rate_per_minute=request.rate_per_minute,
                rate_burst=request.rate_burst,
                max_concurrency=request.max_concurrency,
                action_timeout_seconds=request.action_timeout_seconds,
                access_token=mutation,
            ),
            tts=TTSSettingsDraft(default_voice_id=next(iter(self._voice_ids), "")),
        )
        try:
            self.validate(draft, {})
        except SettingsValidationError:
            return QQConnectionTestResult(
                ok=False,
                code=ConnectionTestCode.VALIDATION_FAILED,
            )
        try:
            status = QQRuntimeStatus.model_validate(
                _controlled_status_payload(current_status)
            )
        except (ValidationError, TypeError, ValueError):
            return QQConnectionTestResult(
                ok=False,
                code=ConnectionTestCode.SERVICE_ERROR,
            )
        return QQConnectionTestResult(
            ok=True,
            code=ConnectionTestCode.SUCCESS,
            status=status,
        )

    async def test_tts(
        self,
        request: TTSTestRequest,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        if not _is_safe_http_url(request.gpt_sovits_url):
            return _result(ConnectionTestCode.VALIDATION_FAILED)

        try:
            base_url = httpx.URL(request.gpt_sovits_url)
            origin = base_url.copy_with(path="/", query=None, fragment=None)

            async def probe() -> bool:
                client = httpx.AsyncClient(
                    timeout=_TTS_NETWORK_TIMEOUT_SECONDS,
                    trust_env=False,
                    transport=transport,
                )
                try:
                    for path in ("/openapi.json", "/"):
                        response = await client.send(
                            client.build_request(
                                "GET",
                                origin.copy_with(path=path),
                            ),
                            stream=True,
                        )
                        try:
                            success = 200 <= response.status_code < 400
                        finally:
                            await _bounded_cleanup(response.aclose())
                        if success:
                            return True
                    return False
                finally:
                    await _cleanup_preserving_primary(client.aclose())

            ok = await asyncio.wait_for(
                probe(),
                timeout=_TTS_NETWORK_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, TimeoutError):
            return _result(ConnectionTestCode.TIMED_OUT)
        except (httpx.RequestError, OSError):
            return _result(ConnectionTestCode.UNREACHABLE)
        except Exception:
            return _result(ConnectionTestCode.SERVICE_ERROR)
        return _result(
            ConnectionTestCode.SUCCESS if ok else ConnectionTestCode.SERVICE_ERROR
        )

    @staticmethod
    def _effective_secret(
        mutation: SecretMutation,
        existing: object,
    ) -> SecretStr | None:
        if mutation.operation is SecretOperation.DELETE:
            return None
        if mutation.operation is SecretOperation.REPLACE:
            if mutation.value is None:
                return None
            value = mutation.value.get_secret_value().strip()
            return SecretStr(value) if value else None
        if isinstance(existing, SecretStr):
            value = existing.get_secret_value().strip()
            return SecretStr(value) if value else None
        if isinstance(existing, str):
            value = existing.strip()
            return SecretStr(value) if value else None
        return None


def _result(code: ConnectionTestCode) -> ConnectionTestResult:
    return ConnectionTestResult(ok=code is ConnectionTestCode.SUCCESS, code=code)


_CLEANUP_TIMEOUT_SECONDS = 0.5
_CLEANUP_CANCEL_GRACE_SECONDS = 0.1


async def _bounded_cleanup(cleanup: Awaitable[None]) -> None:
    """Finish cleanup despite cancellation, with a hard upper time bound."""

    task = asyncio.ensure_future(cleanup)
    cancellation: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CLEANUP_TIMEOUT_SECONDS

    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    if not task.done():
        task.cancel()
        grace_deadline = loop.time() + _CLEANUP_CANCEL_GRACE_SECONDS
        while not task.done() and loop.time() < grace_deadline:
            try:
                await asyncio.wait(
                    {task},
                    timeout=grace_deadline - loop.time(),
                )
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        if not task.done():
            task.add_done_callback(_consume_cleanup_result)
        if cancellation is not None:
            raise cancellation
        raise TimeoutError("resource cleanup timed out")

    try:
        task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation


def _consume_cleanup_result(task: asyncio.Future[None]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _cleanup_preserving_primary(cleanup: Awaitable[None]) -> None:
    primary = sys.exception()
    try:
        await _bounded_cleanup(cleanup)
    except asyncio.CancelledError:
        if isinstance(primary, asyncio.CancelledError):
            raise primary
        raise
    except Exception:
        if primary is None:
            raise


def _nonempty(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_qq_token(value: SecretStr | None) -> bool:
    if value is None:
        return False
    return 16 <= len(value.get_secret_value().strip()) <= 512


def _is_safe_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if "\\" in value or "?" in value or "#" in value or any(
        ord(character) <= 32 or ord(character) == 127
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _validated_snapshot(draft: SettingsDraft) -> ValidatedSettingsSnapshot:
    return ValidatedSettingsSnapshot(
        llm=ValidatedLLMSettings(
            enabled=draft.llm.enabled,
            base_url=draft.llm.base_url,
            model=draft.llm.model,
            timeout_seconds=draft.llm.timeout_seconds,
            max_context_messages=draft.llm.max_context_messages,
            max_context_chars=draft.llm.max_context_chars,
            tool_calling_enabled=draft.llm.tool_calling_enabled,
            api_key=ValidatedSecretMutation(operation=draft.llm.api_key.operation),
        ),
        qq=ValidatedQQSettings(
            enabled=draft.qq.enabled,
            allowed_group_ids=tuple(draft.qq.allowed_group_ids),
            allowed_user_ids=tuple(draft.qq.allowed_user_ids),
            rate_per_minute=draft.qq.rate_per_minute,
            rate_burst=draft.qq.rate_burst,
            max_concurrency=draft.qq.max_concurrency,
            action_timeout_seconds=draft.qq.action_timeout_seconds,
            access_token=ValidatedSecretMutation(
                operation=draft.qq.access_token.operation
            ),
        ),
        tts=ValidatedTTSSettings(
            gpt_sovits_url=draft.tts.gpt_sovits_url,
            default_voice_id=draft.tts.default_voice_id,
            audio_max_age_seconds=draft.tts.audio_max_age_seconds,
        ),
    )


def _controlled_status_payload(
    status: QQRuntimeStatus | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(status, QQRuntimeStatus):
        dumped = status.model_dump(mode="python", warnings="none")
    elif isinstance(status, Mapping):
        dumped = dict(status)
    else:
        raise TypeError("invalid QQ runtime status")

    def field_value(name: str, alias: str) -> object:
        return dumped.get(name, dumped.get(alias))

    return {
        "enabled": field_value("enabled", "enabled"),
        "state": field_value("state", "state"),
        "allowed_group_count": field_value(
            "allowed_group_count",
            "allowedGroupCount",
        ),
        "allowed_user_count": field_value(
            "allowed_user_count",
            "allowedUserCount",
        ),
    }


def _bounded(
    errors: dict[str, str],
    path: str,
    value: object,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        errors[path] = _FIELD_MESSAGE


def _positive_ids(errors: dict[str, str], path: str, values: object) -> None:
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        errors[path] = _FIELD_MESSAGE


__all__ = [
    "ConnectionTestCode",
    "ConnectionTestResult",
    "LLMSettingsDraft",
    "LLMTestRequest",
    "QQConnectionTestResult",
    "QQRuntimeStatus",
    "QQSettingsDraft",
    "QQTestRequest",
    "SettingsDraft",
    "SettingsValidationError",
    "SettingsValidationService",
    "TTSSettingsDraft",
    "TTSTestRequest",
    "ValidatedDraft",
    "ValidatedLLMSettings",
    "ValidatedQQSettings",
    "ValidatedSecretMutation",
    "ValidatedSettingsSnapshot",
    "ValidatedTTSSettings",
]
