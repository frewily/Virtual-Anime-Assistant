"""Tests for the operating-system credential-store adapter."""

import builtins
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings.secrets import (
    KeychainSecretStore,
    KeyringBackend,
    SecretStoreUnavailable,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str | None]] = []

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account))
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.calls.append(("set", service, account))
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account))
        del self.values[(service, account)]


class BrokenKeyring:
    def get_password(self, service: str, account: str) -> str | None:
        raise RuntimeError("private-sentinel")

    def set_password(self, service: str, account: str, value: str) -> None:
        raise RuntimeError("private-sentinel")

    def delete_password(self, service: str, account: str) -> None:
        raise RuntimeError("private-sentinel")


class KeychainSecretStoreTests(unittest.TestCase):
    def assert_redacted(self, error: BaseException) -> None:
        self.assertEqual(str(error), "操作系统凭据库不可用")
        self.assertNotIn("private-sentinel", str(error))
        self.assertNotIn("private-sentinel", repr(error))
        self.assertNotIn(
            "private-sentinel", "".join(traceback.format_exception(error))
        )
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_round_trip_uses_fixed_service_and_reference_as_account(self) -> None:
        backend = FakeKeyring()
        self.assertIsInstance(backend, KeyringBackend)
        store = KeychainSecretStore(backend)
        reference = "llm-api-key:version-1"

        store.set(reference, "very-secret")
        self.assertEqual(store.get(reference), "very-secret")
        store.delete(reference)
        self.assertIsNone(store.get(reference))

        self.assertEqual(
            backend.calls,
            [
                ("set", "VirtualAnimeAssistant/settings", reference),
                ("get", "VirtualAnimeAssistant/settings", reference),
                ("get", "VirtualAnimeAssistant/settings", reference),
                ("delete", "VirtualAnimeAssistant/settings", reference),
                ("get", "VirtualAnimeAssistant/settings", reference),
            ],
        )

    def test_delete_of_missing_reference_is_idempotent(self) -> None:
        backend = FakeKeyring()
        store = KeychainSecretStore(backend)

        store.delete("qq-access-token:missing")

        self.assertEqual(
            backend.calls,
            [
                (
                    "get",
                    "VirtualAnimeAssistant/settings",
                    "qq-access-token:missing",
                )
            ],
        )

    def test_injected_backend_does_not_import_keyring(self) -> None:
        backend = FakeKeyring()
        original_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "keyring":
                raise AssertionError("keyring was imported")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            store = KeychainSecretStore(backend)
            self.assertTrue(store.available())
            store.set("llm-api-key:version-1", "secret")

    def test_unavailable_backend_is_fail_closed_and_redacted(self) -> None:
        store = KeychainSecretStore(BrokenKeyring())

        self.assertIsInstance(store.available(), bool)
        self.assertFalse(store.available())
        for operation in (
            lambda: store.get("llm-api-key:version-1"),
            lambda: store.set("llm-api-key:version-1", "private-sentinel"),
            lambda: store.delete("llm-api-key:version-1"),
        ):
            with self.subTest(operation=operation), self.assertRaises(
                SecretStoreUnavailable
            ) as raised:
                operation()
            self.assert_redacted(raised.exception)

    def test_missing_keyring_module_is_lazy_and_available_returns_false(self) -> None:
        store = KeychainSecretStore()

        with patch(
            "settings.secrets.importlib.import_module",
            side_effect=ImportError("private-sentinel"),
        ):
            self.assertFalse(store.available())
            with self.assertRaises(SecretStoreUnavailable) as raised:
                store.get("llm-api-key:version-1")

        self.assert_redacted(raised.exception)


if __name__ == "__main__":
    unittest.main()
