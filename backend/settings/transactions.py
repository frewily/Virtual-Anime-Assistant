"""Recoverable transactions spanning settings files and credential storage."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import threading
from typing import Protocol, runtime_checkable
from uuid import uuid4

from settings.file_store import (
    SaveJournal,
    is_safe_secret_reference,
)
from settings.models import PersistedSettings
from settings.secrets import SecretStore


class SettingsTransactionError(RuntimeError):
    """A settings transaction failed without exposing secret material."""


@dataclass(frozen=True)
class _SecretSlot:
    logical_name: str
    reference_prefix: str
    section: str
    field: str


_SECRET_SLOTS = (
    _SecretSlot("llm.apiKey", "llm-api-key", "llm", "api_key_ref"),
    _SecretSlot("qq.accessToken", "qq-access-token", "qq", "access_token_ref"),
)
_SLOT_NAMES = frozenset(slot.logical_name for slot in _SECRET_SLOTS)
_TRANSACTION_LOCK = threading.RLock()
_DEFAULT_REFERENCE_ATTEMPTS = 8


@runtime_checkable
class SettingsFileStoreProtocol(Protocol):
    """File-store operations required by the transaction coordinator."""

    def load(self) -> PersistedSettings: ...

    def save(self, settings: PersistedSettings) -> None: ...

    def read_journal(self) -> SaveJournal | None: ...

    def write_journal(self, journal: SaveJournal) -> None: ...

    def delete_journal(self) -> None: ...


class SettingsTransactionCoordinator:
    """Coordinate crash-recoverable updates across file and secret stores."""

    def __init__(
        self,
        file_store: SettingsFileStoreProtocol,
        secret_store: SecretStore,
        reference_factory: Callable[[str], str] | None = None,
    ):
        self._file_store = file_store
        self._secret_store = secret_store
        self._uses_default_reference_factory = reference_factory is None
        self._reference_factory = reference_factory or self._default_reference

    def recover(self) -> None:
        with _TRANSACTION_LOCK:
            try:
                self._recover_locked()
            except Exception:
                raise SettingsTransactionError(
                    "settings transaction recovery failed"
                ) from None

    def save(
        self,
        current: PersistedSettings,
        proposed: PersistedSettings,
        replacements: Mapping[str, str],
    ) -> PersistedSettings:
        with _TRANSACTION_LOCK:
            try:
                self._recover_locked()
                stored_current = self._file_store.load()
                if stored_current != current:
                    raise ValueError("stale settings snapshot")

                replacement_values = self._validate_replacements(replacements)
                final_settings, new_secrets = self._build_final_settings(
                    current, proposed, replacement_values
                )
                target_refs = self._references(final_settings)
                target_ref_set = set(target_refs)
                old_refs = [
                    reference
                    for reference in self._references(current)
                    if reference not in target_ref_set
                ]
                old_refs = list(dict.fromkeys(old_refs))
                new_refs = [reference for reference, _ in new_secrets]
                journal = SaveJournal(
                    transaction_id=uuid4().hex,
                    old_refs=old_refs,
                    new_refs=new_refs,
                    target_refs=target_refs,
                )

                self._file_store.write_journal(journal)
                for reference, value in new_secrets:
                    self._secret_store.set(reference, value)
                self._file_store.save(final_settings)
                for reference in old_refs:
                    self._secret_store.delete(reference)
                self._file_store.delete_journal()
                return final_settings
            except Exception:
                raise SettingsTransactionError("settings transaction failed") from None

    def _recover_locked(self) -> None:
        journal = self._file_store.read_journal()
        if journal is None:
            return

        current = self._file_store.load()
        current_refs = self._references(current)
        committed = current_refs == journal.target_refs
        cleanup_refs = journal.old_refs if committed else journal.new_refs
        if set(cleanup_refs) & set(current_refs):
            raise ValueError("journal cleanup includes an active reference")

        cleanup_failed = False
        for reference in cleanup_refs:
            try:
                self._secret_store.delete(reference)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise SettingsTransactionError("settings transaction recovery failed")
        self._file_store.delete_journal()

    @staticmethod
    def _validate_replacements(replacements: Mapping[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for logical_name, value in replacements.items():
            if logical_name not in _SLOT_NAMES:
                raise ValueError("unknown secret slot")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("empty replacement secret")
            values[logical_name] = value
        return values

    def _build_final_settings(
        self,
        current: PersistedSettings,
        proposed: PersistedSettings,
        replacement_values: Mapping[str, str],
    ) -> tuple[PersistedSettings, list[tuple[str, str]]]:
        payload = proposed.model_dump(mode="python")
        current_refs = set(self._references(current))
        allocated_refs: set[str] = set()
        new_secrets: list[tuple[str, str]] = []

        for slot in _SECRET_SLOTS:
            current_ref = self._get_reference(current, slot)
            proposed_ref = self._get_reference(proposed, slot)
            if slot.logical_name in replacement_values:
                new_ref = self._allocate_reference(
                    slot.reference_prefix, current_refs, allocated_refs
                )
                allocated_refs.add(new_ref)
                payload[slot.section][slot.field] = new_ref
                new_secrets.append((new_ref, replacement_values[slot.logical_name]))
            elif proposed_ref not in (current_ref, None):
                raise ValueError("secret references may only be retained or deleted")

        return PersistedSettings.model_validate(payload), new_secrets

    def _allocate_reference(
        self,
        prefix: str,
        current_refs: set[str],
        allocated_refs: set[str],
    ) -> str:
        attempts = (
            _DEFAULT_REFERENCE_ATTEMPTS
            if self._uses_default_reference_factory
            else 1
        )
        for _ in range(attempts):
            candidate = self._reference_factory(prefix)
            if not is_safe_secret_reference(candidate, prefix):
                raise ValueError("invalid generated secret reference")
            stored_value = self._secret_store.get(candidate)
            collision = (
                candidate in current_refs
                or candidate in allocated_refs
                or stored_value is not None
            )
            if not collision:
                return candidate
        raise ValueError("secret reference collision")

    @staticmethod
    def _get_reference(settings: PersistedSettings, slot: _SecretSlot) -> str | None:
        return getattr(getattr(settings, slot.section), slot.field)

    @classmethod
    def _references(cls, settings: PersistedSettings) -> list[str]:
        return [
            reference
            for slot in _SECRET_SLOTS
            if (reference := cls._get_reference(settings, slot)) is not None
        ]

    @staticmethod
    def _default_reference(prefix: str) -> str:
        return f"{prefix}:{uuid4().hex}"
