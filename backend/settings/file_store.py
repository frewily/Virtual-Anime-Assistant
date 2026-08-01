"""Strict, atomic JSON persistence for non-secret settings state."""

import os
from pathlib import Path
import re
import tempfile
from typing import Literal

from pydantic import ValidationError, model_validator

from settings.models import PersistedModel, PersistedSettings
from settings.paths import SettingsPaths


class SettingsFileError(RuntimeError):
    """A settings-file operation failed without exposing stored content."""


_SECRET_REFERENCE_PATTERN = re.compile(
    r"(?:llm-api-key|qq-access-token):[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)


def is_safe_secret_reference(reference: object, prefix: str | None = None) -> bool:
    """Return whether an opaque reference is safe and belongs to an allowed slot."""

    if not isinstance(reference, str):
        return False
    if _SECRET_REFERENCE_PATTERN.fullmatch(reference) is None:
        return False
    return prefix is None or reference.startswith(f"{prefix}:")


class SaveJournal(PersistedModel):
    """Recoverable record of keyring-reference changes during a settings save."""

    schema_version: Literal[1] = 1
    transaction_id: str
    old_refs: list[str]
    new_refs: list[str]
    target_refs: list[str]

    @model_validator(mode="after")
    def validate_reference_invariants(self) -> "SaveJournal":
        reference_lists = (self.old_refs, self.new_refs, self.target_refs)
        if any(len(references) != len(set(references)) for references in reference_lists):
            raise ValueError("journal references must not contain duplicates")
        if any(
            not is_safe_secret_reference(reference)
            for references in reference_lists
            for reference in references
        ):
            raise ValueError("journal contains an invalid secret reference")

        old_refs = set(self.old_refs)
        new_refs = set(self.new_refs)
        target_refs = set(self.target_refs)
        if old_refs & new_refs or old_refs & target_refs:
            raise ValueError("journal reference sets overlap")
        if not new_refs <= target_refs:
            raise ValueError("new journal references must be targets")

        expected_target_order = sorted(
            self.target_refs,
            key=lambda reference: 0 if reference.startswith("llm-api-key:") else 1,
        )
        target_prefixes = [reference.split(":", 1)[0] for reference in self.target_refs]
        if self.target_refs != expected_target_order or len(target_prefixes) != len(
            set(target_prefixes)
        ):
            raise ValueError("target references do not match logical slot order")
        return self


class SettingsFileStore:
    """Store strict settings and save journals using atomic file replacement."""

    def __init__(self, paths: SettingsPaths):
        self.paths = paths

    def load(self) -> PersistedSettings:
        try:
            payload = self.paths.settings_file.read_bytes()
        except FileNotFoundError:
            return PersistedSettings()
        except OSError:
            raise SettingsFileError("unable to read settings file") from None
        try:
            return PersistedSettings.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise SettingsFileError("unable to read settings file") from None

    def save(self, settings: PersistedSettings) -> None:
        try:
            payload = (
                settings.model_dump_json(by_alias=True, indent=2).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError):
            raise SettingsFileError("unable to serialize settings file") from None
        self._atomic_write(self.paths.settings_file, payload)

    def read_journal(self) -> SaveJournal | None:
        try:
            payload = self.paths.journal_file.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise SettingsFileError("unable to read settings save journal") from None
        try:
            return SaveJournal.model_validate_json(payload)
        except (ValidationError, ValueError):
            raise SettingsFileError("unable to read settings save journal") from None

    def write_journal(self, journal: SaveJournal) -> None:
        try:
            payload = (
                journal.model_dump_json(by_alias=True, indent=2).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError):
            raise SettingsFileError("unable to serialize settings save journal") from None
        self._atomic_write(self.paths.journal_file, payload)

    def delete_journal(self) -> None:
        try:
            self.paths.journal_file.unlink(missing_ok=True)
        except OSError:
            raise SettingsFileError("unable to delete settings save journal") from None

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            self._fsync_directory(path.parent)
        except Exception:
            raise SettingsFileError("unable to atomically write settings file") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
