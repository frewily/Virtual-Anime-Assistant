"""Strict models for settings persisted by the local web interface."""

from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from pydantic.alias_generators import to_camel


class RequestModel(BaseModel):
    """Base model for web requests, including secret-bearing mutations."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=to_camel,
        hide_input_in_errors=True,
    )


class PersistedModel(BaseModel):
    """Base model for closed, strongly typed persisted JSON."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=to_camel,
        strict=True,
    )


class SecretOperation(str, Enum):
    RETAIN = "retain"
    REPLACE = "replace"
    DELETE = "delete"


class SecretMutation(RequestModel):
    """A request to retain, replace, or delete a secret stored in keyring."""

    operation: SecretOperation = SecretOperation.RETAIN
    value: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_secret_for_non_replace_operation(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        operation = data.get("operation", SecretOperation.RETAIN)
        value = data.get("value")
        if (
            operation
            in (SecretOperation.RETAIN, SecretOperation.DELETE, "retain", "delete")
            and value is not None
        ):
            raise ValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": "value_error",
                        "loc": (),
                        "input": {"operation": operation, "value": "**********"},
                        "ctx": {
                            "error": ValueError(
                                "only replace operations may include a secret value"
                            )
                        },
                    }
                ],
            )
        return data

    @model_validator(mode="after")
    def validate_value_for_operation(self) -> "SecretMutation":
        if self.operation is SecretOperation.REPLACE and self.value is None:
            raise ValueError("a replacement secret value is required")
        return self


class AuthRecord(PersistedModel):
    """Password-verifier material, never a plaintext password."""

    algorithm: str
    n: int
    r: int
    p: int
    salt: str
    hash: str


class LLMSettings(PersistedModel):
    enabled: bool = False
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = 60
    max_context_messages: int = 20
    max_context_chars: int = 12000
    tool_calling_enabled: bool = False
    api_key_ref: str | None = None


class QQSettings(PersistedModel):
    enabled: bool = False
    allowed_group_ids: list[int] = Field(default_factory=list)
    allowed_user_ids: list[int] = Field(default_factory=list)
    rate_per_minute: int = 10
    rate_burst: int = 2
    max_concurrency: int = 4
    action_timeout_seconds: int = 10
    access_token_ref: str | None = None


class TTSSettings(PersistedModel):
    gpt_sovits_url: str = "http://127.0.0.1:9880"
    default_voice_id: str = "character_001"
    audio_max_age_seconds: int = 86400


class PersistedSettings(PersistedModel):
    schema_version: Literal[1] = 1
    auth: AuthRecord | None = None
    llm: LLMSettings = Field(default_factory=LLMSettings)
    qq: QQSettings = Field(default_factory=QQSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
