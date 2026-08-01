"""Redacted adapter for secrets stored in the operating-system keychain."""

import importlib
from typing import Protocol, cast, runtime_checkable


_SERVICE_NAME = "VirtualAnimeAssistant/settings"
_AVAILABILITY_REFERENCE = "availability-probe:0"


class SecretStoreUnavailable(RuntimeError):
    """The operating-system credential store could not be used safely."""


@runtime_checkable
class KeyringBackend(Protocol):
    """The small portion of the keyring API required by this adapter."""

    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, value: str) -> None: ...

    def delete_password(self, service: str, account: str) -> None: ...


@runtime_checkable
class SecretStore(Protocol):
    """Minimal reference-based secret-store contract."""

    def available(self) -> bool: ...

    def get(self, reference: str) -> str | None: ...

    def set(self, reference: str, value: str) -> None: ...

    def delete(self, reference: str) -> None: ...


class KeychainSecretStore:
    """Store secret values under opaque version references in ``keyring``."""

    def __init__(self, backend: KeyringBackend | None = None):
        self._backend = backend

    def available(self) -> bool:
        try:
            backend = self._resolve_backend()
            backend.get_password(_SERVICE_NAME, _AVAILABILITY_REFERENCE)
        except Exception:
            return False
        return True

    def get(self, reference: str) -> str | None:
        try:
            return self._resolve_backend().get_password(_SERVICE_NAME, reference)
        except Exception:
            raise SecretStoreUnavailable("操作系统凭据库不可用") from None

    def set(self, reference: str, value: str) -> None:
        try:
            self._resolve_backend().set_password(_SERVICE_NAME, reference, value)
        except Exception:
            raise SecretStoreUnavailable("操作系统凭据库不可用") from None

    def delete(self, reference: str) -> None:
        try:
            backend = self._resolve_backend()
            if backend.get_password(_SERVICE_NAME, reference) is None:
                return
            backend.delete_password(_SERVICE_NAME, reference)
        except Exception:
            raise SecretStoreUnavailable("操作系统凭据库不可用") from None

    def _resolve_backend(self) -> KeyringBackend:
        if self._backend is None:
            try:
                self._backend = cast(
                    KeyringBackend, importlib.import_module("keyring")
                )
            except Exception:
                raise SecretStoreUnavailable("操作系统凭据库不可用") from None
        return self._backend
