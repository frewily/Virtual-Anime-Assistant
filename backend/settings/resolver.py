"""Resolve persisted, keychain, and environment settings into runtime values."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import os
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from channels.onebot.config import (
    OneBotSettings,
    parse_onebot_environment_field,
)
from llm.config import LLMSettings
from settings.models import PersistedSettings
from settings.secrets import SecretStore


class FieldSource(str, Enum):
    DEFAULT = "default"
    PERSISTED = "persisted"
    KEYCHAIN = "keychain"
    ENVIRONMENT = "environment"


@dataclass(frozen=True)
class TTSRuntimeSettings:
    gpt_sovits_url: str
    default_voice_id: str
    audio_max_age_seconds: int


@dataclass(frozen=True)
class RuntimeSettings:
    llm: LLMSettings
    qq: OneBotSettings
    tts: TTSRuntimeSettings


class _PresentationModel(BaseModel):
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


class _FieldPresentation(_PresentationModel):
    source: FieldSource
    read_only: bool = False
    environment_variable: str | None = None


class ValueFieldPresentation(_FieldPresentation):
    value: None | bool | int | str | list[int]
    configured: None = None
    missing: bool = False


class SecretFieldPresentation(_FieldPresentation):
    value: None = None
    configured: bool
    missing: bool = False

    @model_validator(mode="before")
    @classmethod
    def reject_secret_value(cls, data: object) -> object:
        if isinstance(data, Mapping) and data.get("value") is not None:
            raise _redacted_validation_error(
                cls.__name__,
                ("value",),
                "secret presentation values must be null",
            )
        return data


FieldPresentation = ValueFieldPresentation | SecretFieldPresentation
_SECRET_FIELD_PATHS = frozenset({"llm.apiKey", "qq.accessToken"})


class SettingsPresentation(_PresentationModel):
    fields: Mapping[str, FieldPresentation]
    keychain_available: bool

    @model_validator(mode="before")
    @classmethod
    def reject_value_models_for_secret_paths(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        fields = data.get("fields")
        if not isinstance(fields, Mapping):
            return data
        for path in _SECRET_FIELD_PATHS:
            field = fields.get(path)
            if isinstance(field, ValueFieldPresentation) or (
                isinstance(field, Mapping) and field.get("value") is not None
            ):
                raise _redacted_validation_error(
                    cls.__name__,
                    ("fields", path, "value"),
                    "secret paths require secret field presentation",
                )
        return data

    @model_validator(mode="after")
    def validate_field_kinds(self) -> "SettingsPresentation":
        for path, field in self.fields.items():
            if path in _SECRET_FIELD_PATHS:
                if not isinstance(field, SecretFieldPresentation):
                    raise ValueError("secret paths require secret field presentation")
            elif not isinstance(field, ValueFieldPresentation):
                raise ValueError("value paths require value field presentation")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        return self

    @field_serializer("fields")
    def serialize_fields(
        self,
        fields: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        return _redacted_field_payloads(fields)

    def __repr_args__(self):
        return (
            ("fields", _redacted_field_payloads(self.fields)),
            ("keychain_available", self.keychain_available),
        )


@dataclass(frozen=True)
class ResolvedSettings:
    runtime: RuntimeSettings
    presentation: SettingsPresentation


_LLM_ENVIRONMENT_VARIABLES = {
    "enabled": "ASSISTANT_LLM_ENABLED",
    "base_url": "ASSISTANT_LLM_BASE_URL",
    "model": "ASSISTANT_LLM_MODEL",
    "timeout_seconds": "ASSISTANT_LLM_TIMEOUT_SECONDS",
    "max_context_messages": "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES",
    "max_context_chars": "ASSISTANT_LLM_MAX_CONTEXT_CHARS",
    "tool_calling_enabled": "ASSISTANT_LLM_TOOL_CALLING_ENABLED",
    "api_key": "ASSISTANT_LLM_API_KEY",
}
_QQ_ENVIRONMENT_VARIABLES = {
    "enabled": "ASSISTANT_QQ_ENABLED",
    "allowed_group_ids": "ASSISTANT_QQ_ALLOWED_GROUP_IDS",
    "allowed_user_ids": "ASSISTANT_QQ_ALLOWED_USER_IDS",
    "rate_per_minute": "ASSISTANT_QQ_RATE_PER_MINUTE",
    "rate_burst": "ASSISTANT_QQ_RATE_BURST",
    "max_concurrency": "ASSISTANT_QQ_MAX_CONCURRENCY",
    "action_timeout_seconds": "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS",
    "access_token": "ASSISTANT_QQ_ACCESS_TOKEN",
}
_TTS_ENVIRONMENT_VARIABLES = {
    "gpt_sovits_url": "ASSISTANT_GPT_SOVITS_URL",
    "default_voice_id": "ASSISTANT_TTS_DEFAULT_VOICE_ID",
    "audio_max_age_seconds": "ASSISTANT_AUDIO_MAX_AGE_SECONDS",
}


class SettingsResolver:
    """Apply ``defaults < persisted < keychain < environment`` precedence."""

    def __init__(self, secret_store: SecretStore):
        self._secret_store = secret_store

    def resolve(
        self,
        persisted: PersistedSettings,
        environ: Mapping[str, str] | None = None,
    ) -> ResolvedSettings:
        environment = os.environ if environ is None else environ
        keychain_available, secrets = self._read_keychain_secrets(persisted)

        llm_environment = self._llm_environment(persisted, secrets["llm.apiKey"])
        qq_environment = self._qq_environment(persisted, secrets["qq.accessToken"])
        self._overlay_environment(
            llm_environment,
            environment,
            _LLM_ENVIRONMENT_VARIABLES.values(),
        )
        self._overlay_environment(
            qq_environment,
            environment,
            _QQ_ENVIRONMENT_VARIABLES.values(),
        )

        llm = LLMSettings.from_env(llm_environment)
        qq = OneBotSettings.from_env(qq_environment)
        tts = self._resolve_tts(persisted, environment)
        runtime = RuntimeSettings(llm=llm, qq=qq, tts=tts)
        presentation = SettingsPresentation(
            fields=self._presentation_fields(
                persisted,
                runtime,
                environment,
                secrets,
                qq_environment,
            ),
            keychain_available=keychain_available,
        )
        return ResolvedSettings(runtime=runtime, presentation=presentation)

    def _read_keychain_secrets(
        self, persisted: PersistedSettings
    ) -> tuple[bool, dict[str, str | None]]:
        secrets: dict[str, str | None] = {
            "llm.apiKey": None,
            "qq.accessToken": None,
        }
        try:
            available = bool(self._secret_store.available())
        except Exception:
            return False, secrets
        if not available:
            return False, secrets

        references = {
            "llm.apiKey": persisted.llm.api_key_ref,
            "qq.accessToken": persisted.qq.access_token_ref,
        }
        try:
            for name, reference in references.items():
                if reference is not None:
                    secrets[name] = self._secret_store.get(reference)
        except Exception:
            return False, {name: None for name in secrets}
        return True, secrets

    @staticmethod
    def _llm_environment(
        persisted: PersistedSettings, api_key: str | None
    ) -> dict[str, str]:
        llm = persisted.llm
        values = {
            "ASSISTANT_LLM_ENABLED": _format_bool(llm.enabled),
            "ASSISTANT_LLM_TIMEOUT_SECONDS": str(llm.timeout_seconds),
            "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES": str(llm.max_context_messages),
            "ASSISTANT_LLM_MAX_CONTEXT_CHARS": str(llm.max_context_chars),
            "ASSISTANT_LLM_TOOL_CALLING_ENABLED": _format_bool(
                llm.tool_calling_enabled
            ),
        }
        if llm.base_url is not None:
            values["ASSISTANT_LLM_BASE_URL"] = llm.base_url
        if llm.model is not None:
            values["ASSISTANT_LLM_MODEL"] = llm.model
        if api_key is not None:
            values["ASSISTANT_LLM_API_KEY"] = api_key
        return values

    @staticmethod
    def _qq_environment(
        persisted: PersistedSettings, access_token: str | None
    ) -> dict[str, str]:
        qq = persisted.qq
        return {
            "ASSISTANT_QQ_ENABLED": _format_bool(qq.enabled),
            "ASSISTANT_QQ_ALLOWED_GROUP_IDS": _format_ids(qq.allowed_group_ids),
            "ASSISTANT_QQ_ALLOWED_USER_IDS": _format_ids(qq.allowed_user_ids),
            "ASSISTANT_QQ_RATE_PER_MINUTE": str(qq.rate_per_minute),
            "ASSISTANT_QQ_RATE_BURST": str(qq.rate_burst),
            "ASSISTANT_QQ_MAX_CONCURRENCY": str(qq.max_concurrency),
            "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS": str(
                qq.action_timeout_seconds
            ),
            "ASSISTANT_QQ_ACCESS_TOKEN": access_token or "",
        }

    @staticmethod
    def _overlay_environment(
        target: dict[str, str],
        environment: Mapping[str, str],
        variable_names: Iterable[str],
    ) -> None:
        for variable_name in variable_names:
            if variable_name in environment:
                target[variable_name] = environment[variable_name]

    @staticmethod
    def _resolve_tts(
        persisted: PersistedSettings,
        environment: Mapping[str, str],
    ) -> TTSRuntimeSettings:
        tts = persisted.tts
        url_name = _TTS_ENVIRONMENT_VARIABLES["gpt_sovits_url"]
        voice_name = _TTS_ENVIRONMENT_VARIABLES["default_voice_id"]
        age_name = _TTS_ENVIRONMENT_VARIABLES["audio_max_age_seconds"]
        url = environment[url_name] if url_name in environment else tts.gpt_sovits_url
        voice = (
            environment[voice_name]
            if voice_name in environment
            else tts.default_voice_id
        )
        age = (
            environment[age_name]
            if age_name in environment
            else str(tts.audio_max_age_seconds)
        )
        return TTSRuntimeSettings(
            gpt_sovits_url=_nonempty_text(url_name, url, trim_trailing_slashes=True),
            default_voice_id=_nonempty_text(voice_name, voice),
            audio_max_age_seconds=_parse_int(age_name, age),
        )

    @classmethod
    def _presentation_fields(
        cls,
        persisted: PersistedSettings,
        runtime: RuntimeSettings,
        environment: Mapping[str, str],
        secrets: Mapping[str, str | None],
        qq_environment: Mapping[str, str],
    ) -> dict[str, FieldPresentation]:
        fields: dict[str, FieldPresentation] = {}
        cls._add_nonsecret_fields(
            fields,
            {
                "enabled": "llm.enabled",
                "base_url": "llm.baseUrl",
                "model": "llm.model",
                "timeout_seconds": "llm.timeoutSeconds",
                "max_context_messages": "llm.maxContextMessages",
                "max_context_chars": "llm.maxContextChars",
                "tool_calling_enabled": "llm.toolCallingEnabled",
            },
            {
                "enabled": runtime.llm.enabled,
                "base_url": runtime.llm.base_url,
                "model": runtime.llm.model,
                "timeout_seconds": runtime.llm.timeout_seconds,
                "max_context_messages": runtime.llm.max_context_messages,
                "max_context_chars": runtime.llm.max_context_chars,
                "tool_calling_enabled": runtime.llm.tool_calling_enabled,
            },
            persisted.llm.model_fields_set,
            _LLM_ENVIRONMENT_VARIABLES,
            environment,
        )
        cls._add_nonsecret_fields(
            fields,
            {
                "enabled": "qq.enabled",
                "allowed_group_ids": "qq.allowedGroupIds",
                "allowed_user_ids": "qq.allowedUserIds",
                "rate_per_minute": "qq.ratePerMinute",
                "rate_burst": "qq.rateBurst",
                "max_concurrency": "qq.maxConcurrency",
                "action_timeout_seconds": "qq.actionTimeoutSeconds",
            },
            cls._qq_presentation_values(qq_environment),
            persisted.qq.model_fields_set,
            _QQ_ENVIRONMENT_VARIABLES,
            environment,
        )
        cls._add_nonsecret_fields(
            fields,
            {
                "gpt_sovits_url": "tts.gptSovitsUrl",
                "default_voice_id": "tts.defaultVoiceId",
                "audio_max_age_seconds": "tts.audioMaxAgeSeconds",
            },
            {
                "gpt_sovits_url": runtime.tts.gpt_sovits_url,
                "default_voice_id": runtime.tts.default_voice_id,
                "audio_max_age_seconds": runtime.tts.audio_max_age_seconds,
            },
            persisted.tts.model_fields_set,
            _TTS_ENVIRONMENT_VARIABLES,
            environment,
        )
        cls._add_secret_field(
            fields,
            path="llm.apiKey",
            reference=persisted.llm.api_key_ref,
            secret=secrets["llm.apiKey"],
            environment_variable=_LLM_ENVIRONMENT_VARIABLES["api_key"],
            environment=environment,
        )
        cls._add_secret_field(
            fields,
            path="qq.accessToken",
            reference=persisted.qq.access_token_ref,
            secret=secrets["qq.accessToken"],
            environment_variable=_QQ_ENVIRONMENT_VARIABLES["access_token"],
            environment=environment,
        )
        return fields

    @staticmethod
    def _qq_presentation_values(
        qq_environment: Mapping[str, str],
    ) -> dict[str, None | bool | int | str | list[int]]:
        values: dict[str, None | bool | int | str | list[int]] = {}
        for model_field, variable in _QQ_ENVIRONMENT_VARIABLES.items():
            if model_field == "access_token":
                continue
            raw_value = qq_environment.get(variable)
            try:
                value = parse_onebot_environment_field(variable, raw_value)
            except (AttributeError, TypeError, ValueError):
                value = raw_value
            if isinstance(value, frozenset):
                value = sorted(value)
            values[model_field] = value
        return values

    @staticmethod
    def _add_nonsecret_fields(
        fields: dict[str, FieldPresentation],
        paths: Mapping[str, str],
        values: Mapping[str, Any],
        persisted_fields: set[str],
        environment_variables: Mapping[str, str],
        environment: Mapping[str, str],
    ) -> None:
        for model_field, path in paths.items():
            variable = environment_variables[model_field]
            environment_override = variable in environment
            source = (
                FieldSource.ENVIRONMENT
                if environment_override
                else FieldSource.PERSISTED
                if model_field in persisted_fields
                else FieldSource.DEFAULT
            )
            fields[path] = ValueFieldPresentation(
                value=values[model_field],
                source=source,
                read_only=environment_override,
                environment_variable=variable,
            )

    @staticmethod
    def _add_secret_field(
        fields: dict[str, FieldPresentation],
        *,
        path: str,
        reference: str | None,
        secret: str | None,
        environment_variable: str,
        environment: Mapping[str, str],
    ) -> None:
        environment_override = environment_variable in environment
        if environment_override:
            configured = bool(environment[environment_variable].strip())
            source = FieldSource.ENVIRONMENT
            missing = False
        elif reference is not None:
            configured = bool(secret and secret.strip())
            source = FieldSource.KEYCHAIN
            missing = not configured
        else:
            configured = False
            source = FieldSource.DEFAULT
            missing = False
        fields[path] = SecretFieldPresentation(
            value=None,
            source=source,
            read_only=environment_override,
            environment_variable=environment_variable,
            configured=configured,
            missing=missing,
        )


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _redacted_validation_error(
    model_name: str,
    location: tuple[str, ...],
    message: str,
) -> ValidationError:
    return ValidationError.from_exception_data(
        model_name,
        [
            {
                "type": "value_error",
                "loc": location,
                "input": None,
                "ctx": {"error": ValueError(message)},
            }
        ],
    )


def _redacted_field_payloads(
    fields: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    payloads: dict[str, dict[str, object]] = {}
    for path, field in fields.items():
        if path in _SECRET_FIELD_PATHS:
            payload = _redacted_secret_payload(field)
        elif isinstance(field, BaseModel):
            payload = field.model_dump(by_alias=True)
        elif isinstance(field, Mapping):
            payload = dict(field)
        else:
            payload = {}
        payloads[path] = payload
    return payloads


def _redacted_secret_payload(field: object) -> dict[str, object]:
    if isinstance(field, Mapping):
        source = field.get("source", FieldSource.DEFAULT)
        read_only = field.get("readOnly", field.get("read_only", False))
        environment_variable = field.get(
            "environmentVariable",
            field.get("environment_variable"),
        )
        configured = field.get("configured")
        missing = field.get("missing", False)
    else:
        source = getattr(field, "source", FieldSource.DEFAULT)
        read_only = getattr(field, "read_only", False)
        environment_variable = getattr(field, "environment_variable", None)
        configured = getattr(field, "configured", None)
        missing = getattr(field, "missing", False)
    return {
        "source": source,
        "readOnly": read_only,
        "environmentVariable": environment_variable,
        "value": None,
        "configured": configured,
        "missing": missing,
    }


def _format_ids(identifiers: list[int]) -> str:
    return ",".join(str(identifier) for identifier in sorted(set(identifiers)))


def _nonempty_text(
    name: str,
    raw_value: str,
    *,
    trim_trailing_slashes: bool = False,
) -> str:
    try:
        value = raw_value.strip()
        if trim_trailing_slashes:
            value = value.rstrip("/")
    except (AttributeError, TypeError):
        raise ValueError(name) from None
    if not value:
        raise ValueError(name)
    return value


def _parse_int(
    name: str,
    raw_value: str,
) -> int:
    try:
        return int(raw_value.strip())
    except (AttributeError, TypeError, ValueError):
        raise ValueError(name) from None
