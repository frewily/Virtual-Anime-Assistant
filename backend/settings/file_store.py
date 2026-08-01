"""Strict, atomic JSON persistence for non-secret settings state."""

import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import ValidationError

from settings.models import PersistedModel, PersistedSettings
from settings.paths import SettingsPaths


class SettingsFileError(RuntimeError):
    """A settings-file operation failed without exposing stored content."""


class SaveJournal(PersistedModel):
    """Recoverable record of keyring-reference changes during a settings save."""

    schema_version: Literal[1] = 1
    transaction_id: str
    old_refs: list[str]
    new_refs: list[str]
    target_refs: list[str]


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
