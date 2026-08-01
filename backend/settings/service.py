"""Lazy, redacted facade for the local settings interface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import threading
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from core.config_loader import VoiceCatalog, load_voice_catalog
from settings.auth import AuthError, PasswordPolicyError, Session, SettingsAuthService
from settings.file_store import SettingsFileError, SettingsFileStore
from settings.models import (
    LLMSettings,
    PersistedSettings,
    QQSettings,
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


class VoiceSummary(_ResponseModel):
    id: str
    name: str
    description: str


_SERVICE_MESSAGES = {
    "KEYCHAIN_UNAVAILABLE": "操作系统凭据库不可用",
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
            try:
                self._transaction.recover()
            except SettingsTransactionError:
                raise SettingsServiceError("SETTINGS_RECOVERY_FAILED") from None

    def runtime_settings(self) -> RuntimeSettings:
        """Resolve runtime values, falling back on defaults for a corrupt file."""

        try:
            persisted = self._file_store.load()
        except SettingsFileError:
            persisted = PersistedSettings()
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
            except (PasswordPolicyError, AuthError):
                raise
            except Exception:
                raise SettingsServiceError("SETTINGS_AUTH_FAILED") from None

            proposed = current.model_copy(update={"auth": auth_record})
            try:
                self._transaction.save(current, proposed, {})
            except SettingsTransactionError:
                raise SettingsServiceError("SETTINGS_SAVE_FAILED") from None

            try:
                # SettingsAuthService.create_session mutates its registry only after
                # every fallible token-generation step has completed, so an error
                # cannot leave a newly inserted session that lacks a returned token.
                return self._auth.create_session()
            except (KeyboardInterrupt, SystemExit):
                try:
                    self._transaction.save(proposed, current, {})
                except BaseException:
                    # Preserve the original process-control exception even when the
                    # best-effort CAS rollback encounters a conflict or I/O failure.
                    pass
                raise
            except Exception:
                try:
                    self._transaction.save(proposed, current, {})
                except SettingsTransactionError:
                    raise SettingsServiceError(
                        "SETTINGS_SETUP_STATE_UNCERTAIN"
                    ) from None
                raise SettingsServiceError("SETTINGS_AUTH_FAILED") from None

    def login(self, client: str, password: str) -> Session | None:
        persisted = self._load_settings()
        if persisted.auth is None:
            return None
        return self._auth.login(client, password, persisted.auth)

    def logout(self, token: str | None) -> None:
        self._auth.revoke(token)

    def get_config(self) -> SettingsPresentation:
        return self._resolve(self._load_settings()).presentation

    def get_draft(self) -> SettingsDraft:
        return self._draft_from_persisted(self._load_settings())

    def get_voices(self) -> list[VoiceSummary]:
        catalog = self._load_voice_catalog()
        try:
            return [
                VoiceSummary(
                    id=voice["id"],
                    name=voice["name"],
                    description=voice["description"],
                )
                for voice in catalog.voices
            ]
        except Exception:
            raise SettingsServiceError("VOICE_CATALOG_INVALID") from None

    def save(self, draft: SettingsDraft) -> SaveResult:
        """Validate and atomically persist a complete browser draft."""

        with _LIFECYCLE_LOCK:
            current = self._load_settings()
            resolved = self._resolve(current)
            self._reject_environment_mutations(draft, current, resolved.presentation)
            if not resolved.presentation.keychain_available:
                raise SettingsServiceError("KEYCHAIN_UNAVAILABLE")

            catalog = self._load_voice_catalog()
            validation = SettingsValidationService(catalog)
            validated = validation.validate(
                draft,
                {
                    "llm.apiKey": resolved.runtime.llm.api_key,
                    "qq.accessToken": resolved.runtime.qq.access_token,
                },
            )
            proposed = self._proposed_settings(current, validated.draft)
            replacements = self._replacement_values(draft, validated)
            try:
                self._transaction.save(current, proposed, replacements)
            except SettingsTransactionError:
                raise SettingsServiceError("SETTINGS_SAVE_FAILED") from None
            return SaveResult(restart_required=True)

    async def test_llm(
        self,
        request: LLMTestRequest,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_llm(request, transport)

    async def test_qq(
        self,
        request: QQTestRequest,
        current_status: QQRuntimeStatus | Mapping[str, object],
    ) -> QQConnectionTestResult:
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_qq(request, current_status)

    async def test_tts(
        self,
        request: TTSTestRequest,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectionTestResult:
        return await SettingsValidationService(
            self._load_voice_catalog()
        ).test_tts(request, transport)

    def _load_settings(self) -> PersistedSettings:
        try:
            return self._file_store.load()
        except SettingsFileError:
            raise SettingsServiceError("SETTINGS_FILE_INVALID") from None

    def _resolve(self, persisted: PersistedSettings) -> ResolvedSettings:
        try:
            return self._resolver.resolve(persisted, self._environ)
        except SettingsServiceError:
            raise
        except Exception:
            raise SettingsServiceError("SETTINGS_FILE_INVALID") from None

    def _load_voice_catalog(self) -> VoiceCatalog:
        try:
            catalog = self._voice_catalog_loader()
            if not isinstance(catalog, VoiceCatalog):
                raise TypeError
            return catalog
        except Exception:
            raise SettingsServiceError("VOICE_CATALOG_INVALID") from None

    @staticmethod
    def _draft_from_persisted(persisted: PersistedSettings) -> SettingsDraft:
        return SettingsDraft(
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

    @staticmethod
    def _reject_environment_mutations(
        draft: SettingsDraft,
        current: PersistedSettings,
        presentation: SettingsPresentation,
    ) -> None:
        errors: dict[str, str] = {}
        for path, (section, attribute) in _ENVIRONMENT_FIELD_PATHS.items():
            field = presentation.fields[path]
            if field.read_only and getattr(getattr(draft, section), attribute) != getattr(
                getattr(current, section), attribute
            ):
                errors[path] = "环境变量接管的配置不可修改"
        for path, (section, attribute) in _SECRET_DRAFT_PATHS.items():
            field = presentation.fields[path]
            mutation = getattr(getattr(draft, section), attribute)
            if field.read_only and mutation.operation is not SecretOperation.RETAIN:
                errors[path] = "环境变量接管的配置不可修改"
        if errors:
            raise SettingsValidationError(errors) from None

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
    def _replacement_values(draft, validated) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for path, mutation in (
            ("llm.apiKey", draft.llm.api_key),
            ("qq.accessToken", draft.qq.access_token),
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
    "SaveResult",
    "SessionStatus",
    "SettingsService",
    "SettingsServiceError",
    "VoiceSummary",
    "create_settings_service",
]
