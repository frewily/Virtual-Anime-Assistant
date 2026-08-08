"""Lazy, redacted facade for the local settings interface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import hmac
import json
import os
import threading
from typing import Literal, Self

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticSerializationError

from core.config_loader import VoiceCatalog, load_voice_catalog
from settings.auth import (
    AuthError,
    LoginRateLimited,
    PasswordPolicyError,
    Session,
    SettingsAuthService,
)
from settings.file_store import SettingsFileError, SettingsFileStore
from settings.models import (
    LLMSettings,
    PersistedSettings,
    QQSettings,
    SecretMutation,
    SecretOperation,
    TTSSettings,
)
from settings.paths import SettingsPaths
from settings.resolver import (
    ResolvedSettings,
    RuntimeSettings,
    SettingsPresentation,
    SettingsResolver,
)
from settings.secrets import KeychainSecretStore, SecretStore
from settings.transactions import (
    SettingsFileStoreProtocol,
    SettingsTransactionCoordinator,
    SettingsTransactionError,
)
from settings.validation import (
    ConnectionTestCode,
    ConnectionTestResult,
    LLMSettingsDraft,
    LLMTestRequest,
    QQConnectionTestResult,
    QQRuntimeStatus,
    QQSettingsDraft,
    QQTestRequest,
    SettingsDraft,
    SettingsValidationError,
    SettingsValidationService,
    TTSTestRequest,
    TTSSettingsDraft,
)


class VersionedSettingsDraft(SettingsDraft):
    """Editable settings plus an opaque full-snapshot concurrency revision."""

    revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class _ProbeDraft(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
        strict=True,
    )

    revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class LLMProbeDraft(_ProbeDraft):
    base_url: str
    model: str
    api_key: SecretMutation = Field(default_factory=SecretMutation)


class QQProbeDraft(_ProbeDraft):
    enabled: bool = False
    allowed_group_ids: list[int] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    rate_per_minute: int = 10
    rate_burst: int = 2
    max_concurrency: int = 4
    action_timeout_seconds: int = 10
    access_token: SecretMutation = Field(default_factory=SecretMutation)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
        validate_assignment=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("settings responses cannot be updated")
        return super().model_copy(deep=True)


class SessionStatus(_ResponseModel):
    """Safe authentication state for the settings page."""

    initialized: bool
    authenticated: bool
    csrf_token: str | None = Field(default=None, repr=False)
    expires_at: float | None = None


class SaveResult(_ResponseModel):
    restart_required: bool


class ResponseSecretMutation(_ResponseModel):
    operation: Literal[SecretOperation.RETAIN] = SecretOperation.RETAIN


class ResponseLLMSettingsDraft(_ResponseModel):
    enabled: bool
    base_url: str | None
    model: str | None
    timeout_seconds: int
    max_context_messages: int
    max_context_chars: int
    tool_calling_enabled: bool
    api_key: ResponseSecretMutation


class ResponseQQSettingsDraft(_ResponseModel):
    enabled: bool
    allowed_group_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    rate_per_minute: int
    rate_burst: int
    max_concurrency: int
    action_timeout_seconds: int
    access_token: ResponseSecretMutation


class ResponseTTSSettingsDraft(_ResponseModel):
    gpt_sovits_url: str
    default_voice_id: str
    audio_max_age_seconds: int


class SettingsResponseDraft(_ResponseModel):
    revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    llm: ResponseLLMSettingsDraft
    qq: ResponseQQSettingsDraft
    tts: ResponseTTSSettingsDraft


class SettingsConfigSnapshot(_ResponseModel):
    """Safe presentation and browser draft from one persisted snapshot."""

    presentation: SettingsPresentation
    draft: SettingsResponseDraft
    _integrity_seal: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def seal_validated_snapshot(self) -> "SettingsConfigSnapshot":
        self._integrity_seal = self._calculate_integrity_seal()
        return self

    @field_serializer("presentation")
    def serialize_presentation(self, value: object, info) -> dict[str, object]:
        self._verify_integrity()
        return self._safe_snapshot_payload(
            value,
            SettingsPresentation,
            by_alias=bool(info.by_alias),
        )

    @field_serializer("draft")
    def serialize_draft(self, value: object, info) -> dict[str, object]:
        self._verify_integrity()
        return self._safe_snapshot_payload(
            value,
            SettingsResponseDraft,
            by_alias=bool(info.by_alias),
        )

    def _verify_integrity(self) -> None:
        expected = self._integrity_seal
        if not isinstance(expected, str):
            self._serialization_failure()
        actual = self._calculate_integrity_seal()
        if not hmac.compare_digest(expected, actual):
            self._serialization_failure()

    def _calculate_integrity_seal(self) -> str:
        payload: dict[str, object] = {
            "presentation": self._safe_snapshot_payload(
                self.presentation,
                SettingsPresentation,
                by_alias=True,
            ),
            "draft": self._safe_snapshot_payload(
                self.draft,
                SettingsResponseDraft,
                by_alias=True,
            ),
        }
        if "restart_required" in type(self).model_fields:
            restart_required = getattr(self, "restart_required", None)
            if type(restart_required) is not bool:
                self._serialization_failure()
            payload["restartRequired"] = restart_required
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except Exception:
            self._serialization_failure()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _safe_snapshot_payload(
        value: object,
        model_type: type[BaseModel],
        *,
        by_alias: bool,
    ) -> dict[str, object]:
        try:
            payload = (
                value.model_dump(
                    mode="python",
                    by_alias=False,
                    warnings="none",
                )
                if isinstance(value, BaseModel)
                else value
            )
            validated = model_type.model_validate(payload)
            return validated.model_dump(
                mode="json",
                by_alias=by_alias,
                warnings="none",
            )
        except Exception:
            SettingsConfigSnapshot._serialization_failure()

    @staticmethod
    def _serialization_failure() -> None:
        raise PydanticSerializationError(
            "invalid settings response snapshot"
        ) from None


class SettingsSaveSnapshot(SettingsConfigSnapshot):
    restart_required: bool


class VoiceSummary(_ResponseModel):
    id: str
    name: str
    description: str


_SERVICE_MESSAGES = {
    "KEYCHAIN_UNAVAILABLE": "操作系统凭据库不可用",
    "SETTINGS_CONFLICT": "设置已被其他页面更新，请刷新后重试",
    "SETTINGS_ALREADY_INITIALIZED": "设置密码已经初始化",
    "SETTINGS_AUTH_FAILED": "认证操作失败",
    "SETTINGS_FILE_INVALID": "设置文件无效，请修复或移走后重试",
    "SETTINGS_RECOVERY_FAILED": "设置恢复失败",
    "SETTINGS_SAVE_FAILED": "设置保存失败",
    "SETTINGS_SETUP_STATE_UNCERTAIN": "设置初始化状态需要恢复",
    "VOICE_CATALOG_INVALID": "音色目录无效",
}


class SettingsServiceError(RuntimeError):
    """Stable service failure whose representations contain no input values."""

    def __init__(self, code: str):
        safe_code = code if code in _SERVICE_MESSAGES else "SETTINGS_SAVE_FAILED"
        self.code = safe_code
        super().__init__(_SERVICE_MESSAGES[safe_code])

    def to_dict(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": _SERVICE_MESSAGES[self.code],
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
        return f"SettingsServiceError(code={self.code!r})"


_LIFECYCLE_LOCK = threading.RLock()
_ENVIRONMENT_FIELD_PATHS = {
    "llm.enabled": ("llm", "enabled"),
    "llm.baseUrl": ("llm", "base_url"),
    "llm.model": ("llm", "model"),
    "llm.timeoutSeconds": ("llm", "timeout_seconds"),
    "llm.maxContextMessages": ("llm", "max_context_messages"),
    "llm.maxContextChars": ("llm", "max_context_chars"),
    "llm.toolCallingEnabled": ("llm", "tool_calling_enabled"),
    "qq.enabled": ("qq", "enabled"),
    "qq.allowedGroupIds": ("qq", "allowed_group_ids"),
    "qq.allowedUserIds": ("qq", "allowed_user_ids"),
    "qq.ratePerMinute": ("qq", "rate_per_minute"),
    "qq.rateBurst": ("qq", "rate_burst"),
    "qq.maxConcurrency": ("qq", "max_concurrency"),
    "qq.actionTimeoutSeconds": ("qq", "action_timeout_seconds"),
    "tts.gptSovitsUrl": ("tts", "gpt_sovits_url"),
    "tts.defaultVoiceId": ("tts", "default_voice_id"),
    "tts.audioMaxAgeSeconds": ("tts", "audio_max_age_seconds"),
}
_SECRET_DRAFT_PATHS = {
    "llm.apiKey": ("llm", "api_key"),
    "qq.accessToken": ("qq", "access_token"),
}
_ENVIRONMENT_VARIABLES_BY_PATH = {
    "llm.enabled": "ASSISTANT_LLM_ENABLED",
    "llm.baseUrl": "ASSISTANT_LLM_BASE_URL",
    "llm.model": "ASSISTANT_LLM_MODEL",
    "llm.timeoutSeconds": "ASSISTANT_LLM_TIMEOUT_SECONDS",
    "llm.maxContextMessages": "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES",
    "llm.maxContextChars": "ASSISTANT_LLM_MAX_CONTEXT_CHARS",
    "llm.toolCallingEnabled": "ASSISTANT_LLM_TOOL_CALLING_ENABLED",
    "llm.apiKey": "ASSISTANT_LLM_API_KEY",
    "qq.enabled": "ASSISTANT_QQ_ENABLED",
    "qq.allowedGroupIds": "ASSISTANT_QQ_ALLOWED_GROUP_IDS",
    "qq.allowedUserIds": "ASSISTANT_QQ_ALLOWED_USER_IDS",
    "qq.ratePerMinute": "ASSISTANT_QQ_RATE_PER_MINUTE",
    "qq.rateBurst": "ASSISTANT_QQ_RATE_BURST",
    "qq.maxConcurrency": "ASSISTANT_QQ_MAX_CONCURRENCY",
    "qq.actionTimeoutSeconds": "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS",
    "qq.accessToken": "ASSISTANT_QQ_ACCESS_TOKEN",
    "tts.gptSovitsUrl": "ASSISTANT_GPT_SOVITS_URL",
    "tts.defaultVoiceId": "ASSISTANT_TTS_DEFAULT_VOICE_ID",
    "tts.audioMaxAgeSeconds": "ASSISTANT_AUDIO_MAX_AGE_SECONDS",
}


class _FallbackSecretStore:
    """Resolver-only store that performs no platform credential I/O."""

    def available(self) -> bool:
        return False

    def get(self, reference: str) -> None:
        raise RuntimeError("fallback secret access is disabled")

    def set(self, reference: str, value: str) -> None:
        raise RuntimeError("fallback secret access is disabled")

    def delete(self, reference: str) -> None:
        raise RuntimeError("fallback secret access is disabled")


class SettingsService:
    """Compose settings persistence, resolution, validation, and authentication."""

    def __init__(
        self,
        *,
        paths: SettingsPaths,
        file_store: SettingsFileStoreProtocol | None = None,
        secret_store: SecretStore | None = None,
        transaction_coordinator: SettingsTransactionCoordinator | None = None,
        resolver: SettingsResolver | None = None,
        auth_service: SettingsAuthService | None = None,
        voice_catalog_loader: Callable[[], VoiceCatalog] = load_voice_catalog,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self._file_store = file_store or SettingsFileStore(paths)
        self._secret_store = secret_store or KeychainSecretStore()
        self._transaction = transaction_coordinator or SettingsTransactionCoordinator(
            self._file_store, self._secret_store
        )
        self._resolver = resolver or SettingsResolver(self._secret_store)
        self._auth = auth_service or SettingsAuthService()
        self._voice_catalog_loader = voice_catalog_loader
        self._environ = environ

    def recover(self) -> None:
        """Recover an interrupted cross-store transaction."""

        with _LIFECYCLE_LOCK:
            self._load_settings()
            recovery_failed = False
            try:
                self._transaction.recover()
            except SettingsTransactionError:
                recovery_failed = True
            if recovery_failed:
                raise SettingsServiceError("SETTINGS_RECOVERY_FAILED")

    def runtime_settings(self) -> RuntimeSettings:
        """Resolve runtime values, falling back on defaults for a corrupt file."""

        corrupt = False
        try:
            persisted = self._file_store.load()
        except SettingsFileError:
            corrupt = True
            persisted = PersistedSettings()
        if corrupt:
            return self._resolve_with(
                SettingsResolver(_FallbackSecretStore()),
                persisted,
            ).runtime
        return self._resolve(persisted).runtime

    def session_status(self, token: str | None) -> SessionStatus:
        persisted = self._load_settings()
        session = self._auth.get_session(token)
        if persisted.auth is None:
            if session is not None:
                self._auth.revoke(session.token)
            return SessionStatus(initialized=False, authenticated=False)
        if session is None:
            return SessionStatus(initialized=True, authenticated=False)
        return SessionStatus(
            initialized=True,
            authenticated=True,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    def setup(self, password: str) -> Session:
        """Persist the first password verifier before creating a session."""

        with _LIFECYCLE_LOCK:
            current = self._load_settings()
            if current.auth is not None:
                raise SettingsServiceError("SETTINGS_ALREADY_INITIALIZED")
            try:
                auth_record = self._auth.hash_password(password)
            except PasswordPolicyError:
                raise
            except Exception:
                auth_record = None
            if auth_record is None:
                raise SettingsServiceError("SETTINGS_AUTH_FAILED")

            proposed = current.model_copy(update={"auth": auth_record})
            save_failed = False
            try:
                self._transaction.save(current, proposed, {})
            except SettingsTransactionError:
                save_failed = True
            if save_failed:
                raise SettingsServiceError("SETTINGS_SAVE_FAILED")

            rollback_failed = False
            try:
                # SettingsAuthService.create_session mutates its registry only after
                # every fallible token-generation step has completed, so an error
                # cannot leave a newly inserted session that lacks a returned token.
                return self._auth.create_session()
            except BaseException as failure:
                try:
                    self._transaction.save(proposed, current, {})
                except BaseException:
                    rollback_failed = True
                if not isinstance(failure, Exception):
                    # Preserve process-control exceptions even when the best-effort
                    # CAS rollback encounters a conflict or another interruption.
                    raise
            if rollback_failed:
                raise SettingsServiceError("SETTINGS_SETUP_STATE_UNCERTAIN")
            raise SettingsServiceError("SETTINGS_AUTH_FAILED")

    def login(self, client: str, password: str) -> Session | None:
        persisted = self._load_settings()
        if persisted.auth is None:
            return None
        login_failed = False
        try:
            return self._auth.login(client, password, persisted.auth)
        except LoginRateLimited:
            raise
        except AuthError:
            login_failed = True
        if login_failed:
            raise SettingsServiceError("SETTINGS_AUTH_FAILED")
        raise AssertionError("unreachable")

    def logout(self, token: str | None) -> None:
        self._auth.revoke(token)

    def authorize(
        self,
        token: str | None,
        csrf_token: str | None = None,
        *,
        require_csrf: bool = False,
    ) -> tuple[bool, bool]:
        """Validate a live session and, when requested, its CSRF token."""

        try:
            session = self._auth.get_session(token)
        except Exception:
            return False, False
        if session is None:
            return False, False
        if not require_csrf:
            return True, True
        try:
            return True, self._auth.validate_csrf(session, csrf_token)
        except Exception:
            return True, False

    def get_config(self) -> SettingsPresentation:
        return self._resolve(self._load_settings()).presentation

    def get_draft(self) -> VersionedSettingsDraft:
        return self._draft_from_persisted(self._load_settings())

    def get_config_snapshot(self) -> SettingsConfigSnapshot:
        """Return presentation and draft from exactly one disk snapshot."""

        with _LIFECYCLE_LOCK:
            return self._config_snapshot_from_persisted(self._load_settings())

    def get_voices(self) -> list[VoiceSummary]:
        catalog = self._load_voice_catalog()
        summaries: list[VoiceSummary] | None = None
        try:
            summaries = [
                VoiceSummary(
                    id=voice["id"],
                    name=voice["name"],
                    description=voice["description"],
                )
                for voice in catalog.voices
            ]
        except Exception:
            summaries = None
        if summaries is None:
            raise SettingsServiceError("VOICE_CATALOG_INVALID")
        return summaries

    def save(self, draft: VersionedSettingsDraft) -> SaveResult:
        """Validate and atomically persist a complete browser draft."""

        with _LIFECYCLE_LOCK:
            result, _ = self._save_locked(draft)
            return result

    def save_with_snapshot(
        self, draft: VersionedSettingsDraft
    ) -> SettingsSaveSnapshot:
        """Save and return a response derived from the committed proposal."""

        with _LIFECYCLE_LOCK:
            result, proposed = self._save_locked(draft)
            snapshot = self._config_snapshot_from_persisted(proposed)
            return SettingsSaveSnapshot(
                restart_required=result.restart_required,
                presentation=snapshot.presentation,
                draft=snapshot.draft,
            )

    async def test_llm(
        self,
        request: LLMProbeDraft,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        with _LIFECYCLE_LOCK:
            current = self._current_probe_settings(request.revision)
            api_key = self._resolve_probe_secret(
                "llm.apiKey",
                request.api_key,
                current.llm.api_key_ref,
            )
            prepared = LLMTestRequest(
                base_url=request.base_url,
                model=request.model,
                api_key=api_key,
            )
        if api_key is None or not api_key.get_secret_value().strip():
            return ConnectionTestResult(
                ok=False,
                code=ConnectionTestCode.VALIDATION_FAILED,
            )
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_llm(prepared, transport)

    async def test_qq(
        self,
        request: QQProbeDraft,
        current_status: QQRuntimeStatus | Mapping[str, object],
    ) -> QQConnectionTestResult:
        with _LIFECYCLE_LOCK:
            current = self._current_probe_settings(request.revision)
            access_token = self._resolve_probe_secret(
                "qq.accessToken",
                request.access_token,
                current.qq.access_token_ref,
            )
            prepared = QQTestRequest(
                enabled=request.enabled,
                allowed_group_ids=list(request.allowed_group_ids),
                allowed_user_ids=list(request.allowed_user_ids),
                rate_per_minute=request.rate_per_minute,
                rate_burst=request.rate_burst,
                max_concurrency=request.max_concurrency,
                action_timeout_seconds=request.action_timeout_seconds,
                access_token=access_token,
            )
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_qq(prepared, current_status)

    async def test_tts(
        self,
        request: TTSTestRequest,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_tts(request, transport)

    def _load_settings(self) -> PersistedSettings:
        load_failed = False
        try:
            return self._file_store.load()
        except SettingsFileError:
            load_failed = True
        if load_failed:
            raise SettingsServiceError("SETTINGS_FILE_INVALID")
        raise AssertionError("unreachable")

    def _current_probe_settings(self, revision: str) -> PersistedSettings:
        current = self._load_settings()
        if not hmac.compare_digest(revision, self._revision(current)):
            raise SettingsServiceError("SETTINGS_CONFLICT")
        return current

    def _resolve_probe_secret(
        self,
        path: str,
        mutation: SecretMutation,
        reference: str | None,
    ) -> SecretStr | None:
        environment = os.environ if self._environ is None else self._environ
        environment_variable = _ENVIRONMENT_VARIABLES_BY_PATH[path]
        if environment_variable in environment:
            if mutation.operation is not SecretOperation.RETAIN:
                raise SettingsValidationError(
                    {path: "环境变量接管的配置不可修改"}
                ) from None
            value = environment[environment_variable]
            return SecretStr(value) if value.strip() else None
        if mutation.operation is SecretOperation.REPLACE:
            value = mutation.value
            return (
                SecretStr(value.get_secret_value())
                if value is not None
                else None
            )
        if mutation.operation is SecretOperation.DELETE or reference is None:
            return None
        availability = self._secret_store_availability()
        if availability is not True:
            raise SettingsServiceError("KEYCHAIN_UNAVAILABLE")
        return self._read_secret(reference)

    def _save_locked(
        self, draft: VersionedSettingsDraft
    ) -> tuple[SaveResult, PersistedSettings]:
        current = self._load_settings()
        snapshot = self._snapshot_draft(draft)
        if (
            snapshot is None
            or not self._valid_revision(snapshot.revision)
            or not hmac.compare_digest(snapshot.revision, self._revision(current))
        ):
            raise SettingsServiceError("SETTINGS_CONFLICT")
        self._reject_environment_mutations(snapshot, current)

        catalog = self._load_voice_catalog()
        validation = SettingsValidationService(catalog)
        validated = validation.validate(
            snapshot,
            self._existing_secrets_for_validation(snapshot, current),
        )
        proposed = self._proposed_settings(current, validated.draft)
        replacements = self._replacement_values(validated)
        transaction_failed = False
        final_settings: PersistedSettings | None = None
        try:
            try:
                final_settings = self._transaction.save(
                    current, proposed, replacements
                )
            except SettingsTransactionError:
                transaction_failed = True
        finally:
            replacements.clear()
        if transaction_failed or not isinstance(
            final_settings, PersistedSettings
        ):
            raise SettingsServiceError("SETTINGS_SAVE_FAILED")
        return SaveResult(restart_required=True), final_settings

    def _config_snapshot_from_persisted(
        self, persisted: PersistedSettings
    ) -> SettingsConfigSnapshot:
        return SettingsConfigSnapshot(
            presentation=self._resolve(persisted).presentation,
            draft=self._response_draft_from_persisted(persisted),
        )

    @classmethod
    def _response_draft_from_persisted(
        cls, persisted: PersistedSettings
    ) -> SettingsResponseDraft:
        return SettingsResponseDraft(
            revision=cls._revision(persisted),
            llm=ResponseLLMSettingsDraft(
                enabled=persisted.llm.enabled,
                base_url=persisted.llm.base_url,
                model=persisted.llm.model,
                timeout_seconds=persisted.llm.timeout_seconds,
                max_context_messages=persisted.llm.max_context_messages,
                max_context_chars=persisted.llm.max_context_chars,
                tool_calling_enabled=persisted.llm.tool_calling_enabled,
                api_key=ResponseSecretMutation(),
            ),
            qq=ResponseQQSettingsDraft(
                enabled=persisted.qq.enabled,
                allowed_group_ids=tuple(persisted.qq.allowed_group_ids),
                allowed_user_ids=tuple(persisted.qq.allowed_user_ids),
                rate_per_minute=persisted.qq.rate_per_minute,
                rate_burst=persisted.qq.rate_burst,
                max_concurrency=persisted.qq.max_concurrency,
                action_timeout_seconds=persisted.qq.action_timeout_seconds,
                access_token=ResponseSecretMutation(),
            ),
            tts=ResponseTTSSettingsDraft(
                gpt_sovits_url=persisted.tts.gpt_sovits_url,
                default_voice_id=persisted.tts.default_voice_id,
                audio_max_age_seconds=persisted.tts.audio_max_age_seconds,
            ),
        )

    def _resolve(self, persisted: PersistedSettings) -> ResolvedSettings:
        return self._resolve_with(self._resolver, persisted)

    def _resolve_with(
        self,
        resolver: SettingsResolver,
        persisted: PersistedSettings,
    ) -> ResolvedSettings:
        resolution_failed = False
        try:
            return resolver.resolve(persisted, self._environ)
        except Exception:
            resolution_failed = True
        if resolution_failed:
            raise SettingsServiceError("SETTINGS_FILE_INVALID")
        raise AssertionError("unreachable")

    def _load_voice_catalog(self) -> VoiceCatalog:
        catalog: VoiceCatalog | None = None
        try:
            catalog = self._voice_catalog_loader()
            if not isinstance(catalog, VoiceCatalog):
                raise TypeError
        except Exception:
            catalog = None
        if catalog is None:
            raise SettingsServiceError("VOICE_CATALOG_INVALID")
        return catalog

    @classmethod
    def _draft_from_persisted(
        cls,
        persisted: PersistedSettings,
    ) -> VersionedSettingsDraft:
        return VersionedSettingsDraft(
            revision=cls._revision(persisted),
            llm=LLMSettingsDraft(
                enabled=persisted.llm.enabled,
                base_url=persisted.llm.base_url,
                model=persisted.llm.model,
                timeout_seconds=persisted.llm.timeout_seconds,
                max_context_messages=persisted.llm.max_context_messages,
                max_context_chars=persisted.llm.max_context_chars,
                tool_calling_enabled=persisted.llm.tool_calling_enabled,
            ),
            qq=QQSettingsDraft(
                enabled=persisted.qq.enabled,
                allowed_group_ids=list(persisted.qq.allowed_group_ids),
                allowed_user_ids=list(persisted.qq.allowed_user_ids),
                rate_per_minute=persisted.qq.rate_per_minute,
                rate_burst=persisted.qq.rate_burst,
                max_concurrency=persisted.qq.max_concurrency,
                action_timeout_seconds=persisted.qq.action_timeout_seconds,
            ),
            tts=TTSSettingsDraft(
                gpt_sovits_url=persisted.tts.gpt_sovits_url,
                default_voice_id=persisted.tts.default_voice_id,
                audio_max_age_seconds=persisted.tts.audio_max_age_seconds,
            ),
        )

    def _reject_environment_mutations(
        self,
        draft: SettingsDraft,
        current: PersistedSettings,
    ) -> None:
        environment = os.environ if self._environ is None else self._environ
        errors: dict[str, str] = {}
        for path, (section, attribute) in _ENVIRONMENT_FIELD_PATHS.items():
            if _ENVIRONMENT_VARIABLES_BY_PATH[path] in environment and getattr(
                getattr(draft, section), attribute
            ) != getattr(
                getattr(current, section), attribute
            ):
                errors[path] = "环境变量接管的配置不可修改"
        for path, (section, attribute) in _SECRET_DRAFT_PATHS.items():
            mutation = getattr(getattr(draft, section), attribute)
            if (
                _ENVIRONMENT_VARIABLES_BY_PATH[path] in environment
                and mutation.operation is not SecretOperation.RETAIN
            ):
                errors[path] = "环境变量接管的配置不可修改"
        if errors:
            raise SettingsValidationError(errors) from None

    @staticmethod
    def _revision(persisted: PersistedSettings) -> str:
        payload = persisted.model_dump_json(by_alias=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _valid_revision(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _snapshot_draft(
        draft: object,
    ) -> VersionedSettingsDraft | None:
        snapshot: VersionedSettingsDraft | None = None
        if isinstance(draft, VersionedSettingsDraft):
            try:
                snapshot = VersionedSettingsDraft.model_validate(
                    draft.model_dump(
                        mode="python",
                        by_alias=False,
                        warnings="none",
                    )
                )
            except Exception:
                snapshot = None
        return snapshot

    def _existing_secrets_for_validation(
        self,
        draft: SettingsDraft,
        current: PersistedSettings,
    ) -> dict[str, object]:
        environment = os.environ if self._environ is None else self._environ
        results: dict[str, object] = {}
        slots = (
            (
                "llm.apiKey",
                draft.llm.api_key,
                draft.llm.enabled,
                current.llm.api_key_ref,
            ),
            (
                "qq.accessToken",
                draft.qq.access_token,
                draft.qq.enabled,
                current.qq.access_token_ref,
            ),
        )
        for path, mutation, enabled, reference in slots:
            environment_variable = _ENVIRONMENT_VARIABLES_BY_PATH[path]
            if environment_variable in environment:
                results[path] = SecretStr(environment[environment_variable])
                continue
            if mutation.operation is not SecretOperation.RETAIN:
                if self._secret_store_availability() is not True:
                    raise SettingsServiceError("KEYCHAIN_UNAVAILABLE")
                results[path] = None
                continue
            if not enabled:
                results[path] = reference is not None
                continue
            if reference is None:
                results[path] = None
                continue
            availability = self._secret_store_availability()
            if availability is None:
                raise SettingsServiceError("KEYCHAIN_UNAVAILABLE")
            if availability is False:
                results[path] = True
                continue
            results[path] = self._read_secret(reference)
        return results

    def _secret_store_availability(self) -> bool | None:
        try:
            return bool(self._secret_store.available())
        except Exception:
            return None

    def _read_secret(self, reference: str) -> SecretStr | None:
        secret: str | None = None
        failed = False
        try:
            secret = self._secret_store.get(reference)
        except Exception:
            failed = True
        if failed:
            raise SettingsServiceError("KEYCHAIN_UNAVAILABLE")
        return SecretStr(secret) if secret is not None else None

    @staticmethod
    def _proposed_settings(current, validated) -> PersistedSettings:
        return PersistedSettings(
            auth=current.auth,
            llm=LLMSettings(
                enabled=validated.llm.enabled,
                base_url=validated.llm.base_url,
                model=validated.llm.model,
                timeout_seconds=validated.llm.timeout_seconds,
                max_context_messages=validated.llm.max_context_messages,
                max_context_chars=validated.llm.max_context_chars,
                tool_calling_enabled=validated.llm.tool_calling_enabled,
                api_key_ref=(
                    None
                    if validated.llm.api_key.operation is SecretOperation.DELETE
                    else current.llm.api_key_ref
                ),
            ),
            qq=QQSettings(
                enabled=validated.qq.enabled,
                allowed_group_ids=list(validated.qq.allowed_group_ids),
                allowed_user_ids=list(validated.qq.allowed_user_ids),
                rate_per_minute=validated.qq.rate_per_minute,
                rate_burst=validated.qq.rate_burst,
                max_concurrency=validated.qq.max_concurrency,
                action_timeout_seconds=validated.qq.action_timeout_seconds,
                access_token_ref=(
                    None
                    if validated.qq.access_token.operation is SecretOperation.DELETE
                    else current.qq.access_token_ref
                ),
            ),
            tts=TTSSettings(
                gpt_sovits_url=validated.tts.gpt_sovits_url,
                default_voice_id=validated.tts.default_voice_id,
                audio_max_age_seconds=validated.tts.audio_max_age_seconds,
            ),
        )

    @staticmethod
    def _replacement_values(validated) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for path, mutation in (
            ("llm.apiKey", validated.draft.llm.api_key),
            ("qq.accessToken", validated.draft.qq.access_token),
        ):
            if mutation.operation is SecretOperation.REPLACE:
                secret = validated.effective_secret(path)
                if secret is not None:
                    replacements[path] = secret.get_secret_value()
        return replacements


def create_settings_service(paths: SettingsPaths | None = None) -> SettingsService:
    """Construct the facade without touching disk, keychain, or voice files."""

    return SettingsService(paths=paths or SettingsPaths.default())


__all__ = [
    "LLMProbeDraft",
    "QQProbeDraft",
    "SaveResult",
    "SettingsConfigSnapshot",
    "SettingsResponseDraft",
    "SettingsSaveSnapshot",
    "SessionStatus",
    "SettingsService",
    "SettingsServiceError",
    "VersionedSettingsDraft",
    "VoiceSummary",
    "create_settings_service",
]
