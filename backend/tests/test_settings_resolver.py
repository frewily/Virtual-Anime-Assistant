"""Tests for layered runtime settings resolution and redacted presentation."""

import json
import sys
import unittest
import warnings
from pathlib import Path

from pydantic import ValidationError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.onebot.models import QQ_MISCONFIGURED
from settings.models import (
    LLMSettings as PersistedLLMSettings,
    PersistedSettings,
    QQSettings,
    TTSSettings,
)
from settings.resolver import (
    FieldSource,
    SecretFieldPresentation,
    SettingsPresentation,
    SettingsResolver,
    ValueFieldPresentation,
)


class MemorySecretStore:
    def __init__(
        self,
        values: dict[str, str] | None = None,
        *,
        available: bool = True,
        fail_get: bool = False,
    ) -> None:
        self.values = dict(values or {})
        self.is_available = available
        self.fail_get = fail_get
        self.requested_references: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def get(self, reference: str) -> str | None:
        self.requested_references.append(reference)
        if self.fail_get:
            raise RuntimeError("private-keychain-error")
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


class SettingsResolverTests(unittest.TestCase):
    def test_defaults_produce_runtime_defaults_and_default_presentation(self) -> None:
        resolved = SettingsResolver(MemorySecretStore()).resolve(
            PersistedSettings(), {}
        )

        self.assertFalse(resolved.runtime.llm.enabled)
        self.assertIsNone(resolved.runtime.llm.base_url)
        self.assertEqual(resolved.runtime.llm.timeout_seconds, 60)
        self.assertFalse(resolved.runtime.qq.enabled)
        self.assertEqual(resolved.runtime.qq.allowed_group_ids, frozenset())
        self.assertEqual(resolved.runtime.qq.rate_per_minute, 10)
        self.assertEqual(resolved.runtime.tts.gpt_sovits_url, "http://127.0.0.1:9880")
        self.assertEqual(resolved.runtime.tts.default_voice_id, "character_001")
        self.assertEqual(resolved.runtime.tts.audio_max_age_seconds, 86400)
        self.assertTrue(resolved.presentation.keychain_available)
        self.assertEqual(len(resolved.presentation.fields), 19)
        for field in resolved.presentation.fields.values():
            self.assertEqual(field.source, FieldSource.DEFAULT)
            self.assertFalse(field.read_only)
            self.assertFalse(field.missing)

        api_key = resolved.presentation.fields["llm.apiKey"]
        self.assertIsNone(api_key.value)
        self.assertFalse(api_key.configured)
        self.assertEqual(api_key.environment_variable, "ASSISTANT_LLM_API_KEY")

    def test_explicit_persisted_nonsecret_values_are_marked_persisted(self) -> None:
        persisted = PersistedSettings(
            llm=PersistedLLMSettings(
                enabled=True,
                base_url=" https://persisted.example/v1/// ",
                model="persisted-model",
            ),
            qq=QQSettings(
                allowed_group_ids=[300, 100, 300],
                allowed_user_ids=[400, 200],
                rate_per_minute=20,
            ),
            tts=TTSSettings(default_voice_id="persisted-voice"),
        )

        resolved = SettingsResolver(MemorySecretStore()).resolve(persisted, {})

        self.assertEqual(resolved.runtime.llm.base_url, "https://persisted.example/v1")
        self.assertEqual(resolved.runtime.llm.model, "persisted-model")
        self.assertEqual(resolved.runtime.qq.allowed_group_ids, frozenset({100, 300}))
        self.assertEqual(resolved.runtime.qq.allowed_user_ids, frozenset({200, 400}))
        self.assertEqual(resolved.runtime.tts.default_voice_id, "persisted-voice")
        for name in (
            "llm.enabled",
            "llm.baseUrl",
            "llm.model",
            "qq.allowedGroupIds",
            "qq.allowedUserIds",
            "qq.ratePerMinute",
            "tts.defaultVoiceId",
        ):
            self.assertEqual(
                resolved.presentation.fields[name].source,
                FieldSource.PERSISTED,
            )
        self.assertEqual(
            resolved.presentation.fields["qq.allowedGroupIds"].value,
            [100, 300],
        )
        self.assertEqual(
            resolved.presentation.fields["llm.timeoutSeconds"].source,
            FieldSource.DEFAULT,
        )

    def test_keychain_secrets_enter_runtime_but_are_fully_redacted(self) -> None:
        secret_store = MemorySecretStore(
            {
                "llm-api-key:version-1": "private-llm-secret",
                "qq-access-token:version-1": "0123456789abcdef-private-qq-secret",
            }
        )
        persisted = PersistedSettings(
            llm=PersistedLLMSettings(api_key_ref="llm-api-key:version-1"),
            qq=QQSettings(access_token_ref="qq-access-token:version-1"),
        )

        resolved = SettingsResolver(secret_store).resolve(persisted, {})

        self.assertEqual(resolved.runtime.llm.api_key, "private-llm-secret")
        self.assertEqual(
            resolved.runtime.qq.access_token,
            "0123456789abcdef-private-qq-secret",
        )
        for name in ("llm.apiKey", "qq.accessToken"):
            field = resolved.presentation.fields[name]
            self.assertIsNone(field.value)
            self.assertTrue(field.configured)
            self.assertEqual(field.source, FieldSource.KEYCHAIN)
            self.assertFalse(field.read_only)
            self.assertFalse(field.missing)

        rendered = "\n".join(
            (
                repr(resolved),
                repr(resolved.runtime),
                repr(resolved.presentation),
                resolved.presentation.model_dump_json(),
            )
        )
        for forbidden in (
            "private-llm-secret",
            "0123456789abcdef-private-qq-secret",
            "llm-api-key:version-1",
            "qq-access-token:version-1",
            "salt",
            "hash",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_environment_overrides_persisted_and_keychain_layers(self) -> None:
        persisted = PersistedSettings(
            llm=PersistedLLMSettings(
                enabled=False,
                base_url="https://persisted.example/v1",
                model="persisted-model",
                timeout_seconds=45,
                max_context_messages=11,
                max_context_chars=9000,
                tool_calling_enabled=False,
                api_key_ref="llm-api-key:version-1",
            ),
            qq=QQSettings(
                enabled=False,
                allowed_group_ids=[1],
                allowed_user_ids=[2],
                rate_per_minute=10,
                rate_burst=2,
                max_concurrency=4,
                action_timeout_seconds=10,
                access_token_ref="qq-access-token:version-1",
            ),
            tts=TTSSettings(
                gpt_sovits_url="http://persisted:9880",
                default_voice_id="persisted-voice",
                audio_max_age_seconds=100,
            ),
        )
        environ = {
            "ASSISTANT_LLM_ENABLED": "yes",
            "ASSISTANT_LLM_BASE_URL": " https://environment.example/v1/// ",
            "ASSISTANT_LLM_MODEL": "environment-model",
            "ASSISTANT_LLM_TIMEOUT_SECONDS": "30",
            "ASSISTANT_LLM_MAX_CONTEXT_MESSAGES": "12",
            "ASSISTANT_LLM_MAX_CONTEXT_CHARS": "8000",
            "ASSISTANT_LLM_TOOL_CALLING_ENABLED": "on",
            "ASSISTANT_LLM_API_KEY": "environment-llm-secret",
            "ASSISTANT_QQ_ENABLED": "true",
            "ASSISTANT_QQ_ALLOWED_GROUP_IDS": "300,100,300",
            "ASSISTANT_QQ_ALLOWED_USER_IDS": "400,200",
            "ASSISTANT_QQ_RATE_PER_MINUTE": "15",
            "ASSISTANT_QQ_RATE_BURST": "3",
            "ASSISTANT_QQ_MAX_CONCURRENCY": "5",
            "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS": "8",
            "ASSISTANT_QQ_ACCESS_TOKEN": "environment-qq-token",
            "ASSISTANT_GPT_SOVITS_URL": " http://environment:9880/// ",
            "ASSISTANT_TTS_DEFAULT_VOICE_ID": " environment-voice ",
            "ASSISTANT_AUDIO_MAX_AGE_SECONDS": "3600",
        }

        resolved = SettingsResolver(
            MemorySecretStore(
                {
                    "llm-api-key:version-1": "keychain-llm-secret",
                    "qq-access-token:version-1": "keychain-qq-secret",
                }
            )
        ).resolve(persisted, environ)

        self.assertTrue(resolved.runtime.llm.enabled)
        self.assertEqual(
            resolved.runtime.llm.base_url,
            "https://environment.example/v1",
        )
        self.assertEqual(resolved.runtime.llm.api_key, "environment-llm-secret")
        self.assertEqual(resolved.runtime.llm.model, "environment-model")
        self.assertEqual(resolved.runtime.llm.timeout_seconds, 30)
        self.assertEqual(resolved.runtime.llm.max_context_messages, 12)
        self.assertEqual(resolved.runtime.llm.max_context_chars, 8000)
        self.assertTrue(resolved.runtime.llm.tool_calling_enabled)
        self.assertTrue(resolved.runtime.qq.enabled)
        self.assertEqual(resolved.runtime.qq.access_token, "environment-qq-token")
        self.assertEqual(resolved.runtime.qq.allowed_group_ids, frozenset({100, 300}))
        self.assertEqual(resolved.runtime.qq.allowed_user_ids, frozenset({200, 400}))
        self.assertEqual(resolved.runtime.qq.rate_per_minute, 15)
        self.assertEqual(resolved.runtime.qq.rate_burst, 3)
        self.assertEqual(resolved.runtime.qq.max_concurrency, 5)
        self.assertEqual(resolved.runtime.qq.action_timeout_seconds, 8)
        self.assertEqual(resolved.runtime.tts.gpt_sovits_url, "http://environment:9880")
        self.assertEqual(resolved.runtime.tts.default_voice_id, "environment-voice")
        self.assertEqual(resolved.runtime.tts.audio_max_age_seconds, 3600)

        for field in resolved.presentation.fields.values():
            self.assertEqual(field.source, FieldSource.ENVIRONMENT)
            self.assertTrue(field.read_only)
            self.assertIsNotNone(field.environment_variable)
        self.assertIsNone(resolved.presentation.fields["llm.apiKey"].value)
        self.assertTrue(resolved.presentation.fields["llm.apiKey"].configured)
        self.assertIsNone(resolved.presentation.fields["qq.accessToken"].value)
        self.assertTrue(resolved.presentation.fields["qq.accessToken"].configured)
        rendered = resolved.presentation.model_dump_json()
        self.assertNotIn("environment-llm-secret", rendered)
        self.assertNotIn("environment-qq-token", rendered)

    def test_empty_secret_environment_values_are_read_only_overrides(self) -> None:
        persisted = PersistedSettings(
            llm=PersistedLLMSettings(api_key_ref="llm-api-key:version-1"),
            qq=QQSettings(access_token_ref="qq-access-token:version-1"),
        )

        resolved = SettingsResolver(
            MemorySecretStore(
                {
                    "llm-api-key:version-1": "keychain-llm-secret",
                    "qq-access-token:version-1": "keychain-qq-secret",
                }
            )
        ).resolve(
            persisted,
            {
                "ASSISTANT_LLM_API_KEY": "   ",
                "ASSISTANT_QQ_ACCESS_TOKEN": "",
            },
        )

        self.assertIsNone(resolved.runtime.llm.api_key)
        self.assertEqual(resolved.runtime.qq.access_token, "")
        for name in ("llm.apiKey", "qq.accessToken"):
            field = resolved.presentation.fields[name]
            self.assertEqual(field.source, FieldSource.ENVIRONMENT)
            self.assertTrue(field.read_only)
            self.assertFalse(field.configured)
            self.assertFalse(field.missing)

    def test_invalid_qq_field_does_not_replace_other_presentation_values(self) -> None:
        persisted = PersistedSettings(
            qq=QQSettings(
                allowed_group_ids=[123],
                rate_burst=7,
            )
        )

        resolved = SettingsResolver(MemorySecretStore()).resolve(
            persisted,
            {"ASSISTANT_QQ_RATE_PER_MINUTE": "not-an-int"},
        )

        self.assertEqual(resolved.runtime.qq.configuration_error, QQ_MISCONFIGURED)
        allowed_groups = resolved.presentation.fields["qq.allowedGroupIds"]
        self.assertEqual(allowed_groups.value, [123])
        self.assertEqual(allowed_groups.source, FieldSource.PERSISTED)
        rate_burst = resolved.presentation.fields["qq.rateBurst"]
        self.assertEqual(rate_burst.value, 7)
        self.assertEqual(rate_burst.source, FieldSource.PERSISTED)
        invalid_rate = resolved.presentation.fields["qq.ratePerMinute"]
        self.assertEqual(invalid_rate.value, "not-an-int")
        self.assertEqual(invalid_rate.source, FieldSource.ENVIRONMENT)
        self.assertTrue(invalid_rate.read_only)

    def test_qq_presentation_parses_valid_fields_during_runtime_fallback(self) -> None:
        resolved = SettingsResolver(MemorySecretStore()).resolve(
            PersistedSettings(),
            {
                "ASSISTANT_QQ_ENABLED": "yes",
                "ASSISTANT_QQ_ALLOWED_GROUP_IDS": "3,1,3",
                "ASSISTANT_QQ_RATE_PER_MINUTE": "not-an-int",
            },
        )

        self.assertEqual(resolved.runtime.qq.configuration_error, QQ_MISCONFIGURED)
        enabled = resolved.presentation.fields["qq.enabled"]
        self.assertIs(enabled.value, True)
        self.assertEqual(enabled.source, FieldSource.ENVIRONMENT)
        allowed_groups = resolved.presentation.fields["qq.allowedGroupIds"]
        self.assertEqual(allowed_groups.value, [1, 3])
        self.assertEqual(allowed_groups.source, FieldSource.ENVIRONMENT)

    def test_missing_references_and_unavailable_keychain_are_reported(self) -> None:
        persisted = PersistedSettings(
            llm=PersistedLLMSettings(api_key_ref="llm-api-key:missing"),
            qq=QQSettings(access_token_ref="qq-access-token:missing"),
        )

        missing = SettingsResolver(MemorySecretStore()).resolve(persisted, {})
        self.assertTrue(missing.presentation.keychain_available)
        for name in ("llm.apiKey", "qq.accessToken"):
            field = missing.presentation.fields[name]
            self.assertEqual(field.source, FieldSource.KEYCHAIN)
            self.assertFalse(field.configured)
            self.assertTrue(field.missing)

        unavailable = SettingsResolver(
            MemorySecretStore(
                {
                    "llm-api-key:missing": "private-secret",
                    "qq-access-token:missing": "private-token",
                },
                available=False,
            )
        ).resolve(persisted, {})
        self.assertFalse(unavailable.presentation.keychain_available)
        self.assertIsNone(unavailable.runtime.llm.api_key)
        self.assertEqual(unavailable.runtime.qq.access_token, "")
        for name in ("llm.apiKey", "qq.accessToken"):
            self.assertTrue(unavailable.presentation.fields[name].missing)

        faulting_store = MemorySecretStore(fail_get=True)
        faulting = SettingsResolver(faulting_store).resolve(persisted, {})
        self.assertFalse(faulting.presentation.keychain_available)
        self.assertIsNone(faulting.runtime.llm.api_key)
        self.assertEqual(faulting.runtime.qq.access_token, "")
        for name in ("llm.apiKey", "qq.accessToken"):
            field = faulting.presentation.fields[name]
            self.assertFalse(field.configured)
            self.assertTrue(field.missing)
        rendered = repr(faulting) + faulting.presentation.model_dump_json()
        self.assertNotIn("private-keychain-error", rendered)
        self.assertNotIn("llm-api-key:missing", rendered)
        self.assertNotIn("qq-access-token:missing", rendered)

    def test_invalid_values_preserve_parser_semantics_without_input_leaks(self) -> None:
        private_input = "private-invalid-value"
        llm_cases = (
            ("ASSISTANT_LLM_ENABLED", private_input),
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", private_input),
            ("ASSISTANT_LLM_TIMEOUT_SECONDS", "301"),
        )
        for variable, value in llm_cases:
            with self.subTest(variable=variable, value=value):
                with self.assertRaises(ValueError) as raised:
                    SettingsResolver(MemorySecretStore()).resolve(
                        PersistedSettings(), {variable: value}
                    )
                self.assertEqual(str(raised.exception), variable)
                self.assertNotIn(private_input, str(raised.exception))

        tts_cases = (
            ("ASSISTANT_GPT_SOVITS_URL", "   "),
            ("ASSISTANT_TTS_DEFAULT_VOICE_ID", "   "),
            ("ASSISTANT_AUDIO_MAX_AGE_SECONDS", private_input),
        )
        for variable, value in tts_cases:
            with self.subTest(variable=variable, value=value):
                with self.assertRaises(ValueError) as raised:
                    SettingsResolver(MemorySecretStore()).resolve(
                        PersistedSettings(), {variable: value}
                    )
                self.assertEqual(str(raised.exception), variable)
                self.assertNotIn(private_input, str(raised.exception))

        qq = SettingsResolver(MemorySecretStore()).resolve(
            PersistedSettings(),
            {
                "ASSISTANT_QQ_ENABLED": "true",
                "ASSISTANT_QQ_ACCESS_TOKEN": "0123456789abcdef",
                "ASSISTANT_QQ_ALLOWED_GROUP_IDS": private_input,
            },
        )
        self.assertEqual(qq.runtime.qq.configuration_error, QQ_MISCONFIGURED)
        self.assertNotIn(private_input, repr(qq.runtime.qq))

    def test_audio_max_age_accepts_any_integer_without_an_artificial_cap(self) -> None:
        for value in (-1, 0, 2_592_001):
            with self.subTest(value=value):
                resolved = SettingsResolver(MemorySecretStore()).resolve(
                    PersistedSettings(),
                    {"ASSISTANT_AUDIO_MAX_AGE_SECONDS": str(value)},
                )

                self.assertEqual(resolved.runtime.tts.audio_max_age_seconds, value)

    def test_secret_presentation_rejects_values_without_leaking_them(self) -> None:
        secret = "probe-secret"

        with self.assertRaises(ValidationError) as raised:
            SecretFieldPresentation(
                value=secret,
                source=FieldSource.ENVIRONMENT,
                read_only=True,
                environment_variable="ASSISTANT_LLM_API_KEY",
                configured=True,
            )

        error = raised.exception
        for rendered in (
            str(error),
            repr(error),
            str(error.errors()),
            error.json(),
        ):
            self.assertNotIn(secret, rendered)

    def test_secret_presentation_is_immutable_after_construction(self) -> None:
        secret = "probe-secret"
        field = SecretFieldPresentation(
            source=FieldSource.DEFAULT,
            configured=False,
        )

        with self.assertRaises((ValidationError, TypeError)):
            field.value = secret

        self.assertNotIn(secret, repr(field))
        self.assertNotIn(secret, field.model_dump_json())

    def test_entire_presentation_rejects_secret_path_as_value_field(self) -> None:
        secret = "probe-secret"
        unsafe_field = ValueFieldPresentation(
            value=secret,
            source=FieldSource.ENVIRONMENT,
            read_only=True,
            environment_variable="ASSISTANT_LLM_API_KEY",
        )

        with self.assertRaises(ValidationError) as raised:
            SettingsPresentation(
                fields={"llm.apiKey": unsafe_field},
                keychain_available=True,
            )

        error = raised.exception
        for rendered in (
            str(error),
            repr(error),
            str(error.errors()),
            error.json(),
        ):
            self.assertNotIn(secret, rendered)

    def test_presentation_fields_are_deeply_immutable(self) -> None:
        secret = "probe-secret"
        presentation = SettingsPresentation(
            fields={
                "llm.apiKey": SecretFieldPresentation(
                    source=FieldSource.DEFAULT,
                    configured=False,
                )
            },
            keychain_available=True,
        )
        unsafe = ValueFieldPresentation(
            value=secret,
            source=FieldSource.DEFAULT,
        )

        with self.assertRaises(TypeError):
            presentation.fields["llm.apiKey"] = unsafe
        with self.assertRaises((ValidationError, TypeError)):
            presentation.fields = {"llm.apiKey": unsafe}

        self.assertNotIn(secret, repr(presentation))
        self.assertNotIn(secret, presentation.model_dump_json())

    def test_bypassed_model_copy_is_redacted_by_repr_and_serializers(self) -> None:
        secret = "probe-secret"
        presentation = SettingsPresentation(
            fields={},
            keychain_available=True,
        )
        unsafe_value = ValueFieldPresentation(
            value=secret,
            source=FieldSource.ENVIRONMENT,
            read_only=True,
            environment_variable="ASSISTANT_LLM_API_KEY",
        )
        unsafe_secret = SecretFieldPresentation(
            source=FieldSource.ENVIRONMENT,
            read_only=True,
            environment_variable="ASSISTANT_LLM_API_KEY",
            configured=True,
        ).model_copy(update={"value": secret})
        bypassed = presentation.model_copy(
            update={
                "fields": {
                    "llm.apiKey": unsafe_secret,
                    "qq.accessToken": unsafe_value,
                }
            }
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = bypassed.model_dump()
            dumped_json = bypassed.model_dump_json()

        self.assertEqual(caught, [])
        self.assertIsInstance(dumped["fields"], dict)
        self.assertIsInstance(json.loads(dumped_json)["fields"], dict)
        for path in ("llm.apiKey", "qq.accessToken"):
            self.assertIsNone(dumped["fields"][path]["value"])
            self.assertIsNone(json.loads(dumped_json)["fields"][path]["value"])
        self.assertNotIn(secret, repr(bypassed))
        self.assertNotIn(secret, dumped_json)

    def test_value_presentation_is_strict_json_safe_and_forbids_extra(self) -> None:
        boolean = ValueFieldPresentation(
            value=True,
            source=FieldSource.DEFAULT,
            read_only=True,
            environment_variable="ASSISTANT_QQ_ENABLED",
        )
        self.assertIs(boolean.value, True)
        self.assertEqual(
            set(boolean.model_dump()),
            {
                "value",
                "source",
                "readOnly",
                "environmentVariable",
                "configured",
                "missing",
            },
        )
        self.assertEqual(
            ValueFieldPresentation(value=1, source=FieldSource.DEFAULT).value,
            1,
        )
        self.assertEqual(
            ValueFieldPresentation(
                value=[2, 1], source=FieldSource.DEFAULT
            ).value,
            [2, 1],
        )
        with self.assertRaises(ValidationError):
            ValueFieldPresentation(
                value=["1"],
                source=FieldSource.DEFAULT,
            )
        with self.assertRaises(ValidationError):
            ValueFieldPresentation(
                value=1,
                source=FieldSource.DEFAULT,
                unexpected=True,
            )

    def test_loaded_full_document_marks_serialized_defaults_as_persisted(self) -> None:
        loaded = PersistedSettings.model_validate_json(
            PersistedSettings().model_dump_json(by_alias=True)
        )

        resolved = SettingsResolver(MemorySecretStore()).resolve(loaded, {})

        self.assertTrue(
            all(
                field.source is FieldSource.PERSISTED
                for name, field in resolved.presentation.fields.items()
                if name not in {"llm.apiKey", "qq.accessToken"}
            )
        )


if __name__ == "__main__":
    unittest.main()
