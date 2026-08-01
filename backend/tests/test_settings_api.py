from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app
from settings.auth import LoginRateLimited, PasswordPolicyError, Session
from settings.service import SaveResult, SettingsServiceError, VersionedSettingsDraft
from settings.validation import (
    ConnectionTestCode,
    ConnectionTestResult,
    QQConnectionTestResult,
    SettingsValidationError,
)


BASE_URL = "http://127.0.0.1:8080"
ORIGIN = BASE_URL
COOKIE_NAME = "vaa_settings_session"


def draft_payload() -> dict[str, object]:
    return {
        "revision": "a" * 64,
        "llm": {
            "enabled": False,
            "baseUrl": None,
            "model": None,
            "timeoutSeconds": 60,
            "maxContextMessages": 20,
            "maxContextChars": 12000,
            "toolCallingEnabled": False,
            "apiKey": {"operation": "retain"},
        },
        "qq": {
            "enabled": False,
            "allowedGroupIds": [],
            "allowedUserIds": [],
            "ratePerMinute": 10,
            "rateBurst": 2,
            "maxConcurrency": 4,
            "actionTimeoutSeconds": 10,
            "accessToken": {"operation": "retain"},
        },
        "tts": {
            "gptSovitsUrl": "http://127.0.0.1:9880",
            "defaultVoiceId": "character_001",
            "audioMaxAgeSeconds": 86400,
        },
    }


class FakeSettingsService:
    def __init__(self) -> None:
        self.initialized = False
        self.sessions: dict[str, Session] = {}
        self.calls: list[tuple[str, object]] = []
        self.presentation = {
            "fields": {
                "llm.apiKey": {
                    "source": "default",
                    "readOnly": False,
                    "environmentVariable": None,
                    "value": None,
                    "configured": False,
                    "missing": False,
                }
            },
            "keychainAvailable": True,
        }
        self.draft = VersionedSettingsDraft.model_validate(draft_payload())

    def session_status(self, token):
        session = self.sessions.get(token)
        return {
            "initialized": self.initialized,
            "authenticated": session is not None,
            "csrfToken": session.csrf_token if session else None,
            "expiresAt": session.expires_at if session else None,
        }

    def setup(self, password):
        self.calls.append(("setup", password))
        self.initialized = True
        session = Session("session-token", "csrf-token", 1800.0)
        self.sessions[session.token] = session
        return session

    def login(self, client, password):
        self.calls.append(("login", (client, password)))
        if password != "long-enough-password":
            return None
        session = Session("login-token", "login-csrf", 1900.0)
        self.sessions[session.token] = session
        return session

    def authorize(self, token, csrf_token=None, *, require_csrf=False):
        session = self.sessions.get(token)
        if session is None:
            return False, False
        return True, not require_csrf or csrf_token == session.csrf_token

    def logout(self, token):
        self.calls.append(("logout", token))
        self.sessions.pop(token, None)

    def get_config(self):
        return self.presentation

    def get_draft(self):
        return self.draft

    def save(self, draft):
        self.calls.append(("save", draft))
        return SaveResult(restart_required=True)

    def get_voices(self):
        return [{"id": "character_001", "name": "默认音色", "description": "温柔"}]

    async def test_llm(self, request):
        self.calls.append(("test_llm", request))
        return ConnectionTestResult(ok=True, code=ConnectionTestCode.SUCCESS)

    async def test_qq(self, request, current_status):
        self.calls.append(("test_qq", (request, current_status)))
        return QQConnectionTestResult(ok=True, code=ConnectionTestCode.SUCCESS)

    async def test_tts(self, request):
        self.calls.append(("test_tts", request))
        return ConnectionTestResult(ok=True, code=ConnectionTestCode.SUCCESS)


class SettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeSettingsService()
        self.app = create_app(runtime_instance=Mock(), settings_service=self.service)
        self.client = TestClient(
            self.app, base_url=BASE_URL, client=("127.0.0.1", 50000)
        )

    def tearDown(self) -> None:
        self.client.close()

    def _origin(self) -> dict[str, str]:
        return {"Origin": ORIGIN}

    def _authenticated(self) -> dict[str, str]:
        self.service.initialized = True
        session = Session("valid-token", "valid-csrf", 2000.0)
        self.service.sessions[session.token] = session
        self.client.cookies.set(COOKIE_NAME, session.token)
        return {"Origin": ORIGIN, "X-CSRF-Token": session.csrf_token}

    def assert_safe_headers(self, response) -> None:
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        csp = response.headers["content-security-policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_security_rejects_remote_client_and_does_not_trust_forwarded_headers(self) -> None:
        remote = TestClient(
            self.app,
            base_url=BASE_URL,
            client=("203.0.113.7", 50000),
        )
        response = remote.get(
            "/api/settings/session",
            headers={"Forwarded": "for=127.0.0.1", "X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "SETTINGS_ACCESS_DENIED")
        self.assertNotIn("203.0.113.7", response.text)
        self.assert_safe_headers(response)
        remote.close()

    def test_security_path_matching_has_segment_boundaries(self) -> None:
        remote = TestClient(
            self.app,
            base_url=BASE_URL,
            client=("203.0.113.7", 50000),
        )
        for path in (
            "/api/settingsevil",
            "/settings-evil",
            "/settings.css",
        ):
            with self.subTest(path=path):
                response = remote.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("content-security-policy", response.headers)
        protected = remote.get("/api/settings/missing")
        self.assertEqual(protected.status_code, 403)
        self.assert_safe_headers(protected)
        remote.close()

    def test_host_must_be_one_exact_header(self) -> None:
        bad_hosts = [
            "localhost:8080",
            "127.0.0.1",
            "127.0.0.1:8081",
            "127.0.0.1.:8080",
            "user@127.0.0.1:8080",
            "127.0.0.1:8080,evil",
            " 127.0.0.1:8080",
        ]
        for host in bad_hosts:
            with self.subTest(host=host):
                response = self.client.get("/api/settings/session", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
        repeated = self.client.get(
            "/api/settings/session",
            headers=[("Host", "127.0.0.1:8080"), ("Host", "127.0.0.1:8080")],
        )
        self.assertEqual(repeated.status_code, 403)

    def test_state_changes_require_one_exact_origin_including_setup_and_login(self) -> None:
        variants = [
            None,
            "null",
            "http://localhost:8080",
            "https://127.0.0.1:8080",
            "http://user@127.0.0.1:8080",
            "http://127.0.0.1:8080/?x=1",
            "http://127.0.0.1:8080/#fragment",
        ]
        for origin in variants:
            headers = {} if origin is None else {"Origin": origin}
            with self.subTest(origin=origin):
                response = self.client.post(
                    "/api/settings/setup",
                    headers=headers,
                    json={"password": "long-enough-password"},
                )
                self.assertEqual(response.status_code, 403)
        repeated = self.client.post(
            "/api/settings/login",
            headers=[("Origin", ORIGIN), ("Origin", ORIGIN)],
            json={"password": "long-enough-password"},
        )
        self.assertEqual(repeated.status_code, 403)

    def test_setup_login_session_and_logout_cookie_contract(self) -> None:
        setup = self.client.post(
            "/api/settings/setup",
            headers=self._origin(),
            json={"password": "long-enough-password"},
        )
        self.assertEqual(setup.status_code, 200)
        self.assertEqual(setup.json()["csrfToken"], "csrf-token")
        self.assertNotIn("session-token", setup.text)
        cookie = setup.headers["set-cookie"]
        self.assertIn(f"{COOKIE_NAME}=session-token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn("Secure", cookie)

        status = self.client.get("/api/settings/session")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["authenticated"])
        self.assertNotIn("session-token", status.text)

        logout = self.client.post(
            "/api/settings/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(logout.status_code, 200)
        self.assertIn(f"{COOKIE_NAME}=", logout.headers["set-cookie"])
        self.assertIn("Path=/", logout.headers["set-cookie"])

        login = self.client.post(
            "/api/settings/login",
            headers=self._origin(),
            json={"password": "long-enough-password"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["csrfToken"], "login-csrf")

    def test_authenticated_routes_distinguish_unauthorized_and_csrf_failure(self) -> None:
        self.service.initialized = True
        response = self.client.get("/api/settings/config")
        self.assertEqual(response.status_code, 401)
        headers = self._authenticated()
        response = self.client.put(
            "/api/settings/config",
            headers={"Origin": ORIGIN},
            json=draft_payload(),
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.put(
            "/api/settings/config",
            headers={**headers, "X-CSRF-Token": "wrong"},
            json=draft_payload(),
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("wrong", response.text)
        duplicate = self.client.put(
            "/api/settings/config",
            headers=[
                ("Origin", ORIGIN),
                ("X-CSRF-Token", "valid-csrf"),
                ("X-CSRF-Token", "valid-csrf"),
            ],
            json=draft_payload(),
        )
        self.assertEqual(duplicate.status_code, 403)

    def test_config_round_trip_is_redacted_and_save_returns_fresh_snapshot(self) -> None:
        headers = self._authenticated()
        fetched = self.client.get("/api/settings/config")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["draft"]["revision"], "a" * 64)
        self.assertIsNone(fetched.json()["presentation"]["fields"]["llm.apiKey"]["value"])

        saved = self.client.put(
            "/api/settings/config", headers=headers, json=draft_payload()
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["restartRequired"])
        self.assertEqual(saved.json()["draft"]["revision"], "a" * 64)
        self.assertEqual(self.service.calls[-1][0], "save")

    def test_voices_and_connection_tests_delegate_under_authentication(self) -> None:
        headers = self._authenticated()
        voices = self.client.get("/api/settings/voices")
        self.assertEqual(voices.status_code, 200)
        self.assertEqual(voices.json()[0]["id"], "character_001")

        cases = (
            ("llm", {"baseUrl": "http://127.0.0.1:11434/v1", "model": "m", "apiKey": "private"}),
            ("qq", {"enabled": False}),
            ("tts", {"gptSovitsUrl": "http://127.0.0.1:9880"}),
        )
        for name, body in cases:
            with self.subTest(name=name):
                response = self.client.post(
                    f"/api/settings/test/{name}", headers=headers, json=body
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])
        self.assertEqual([call[0] for call in self.service.calls[-3:]], ["test_llm", "test_qq", "test_tts"])

    def test_validation_errors_are_stable_and_do_not_echo_malicious_input(self) -> None:
        headers = self._authenticated()
        secret = "DO-NOT-ECHO-THIS-SECRET"
        payload = draft_payload()
        payload["llm"]["enabled"] = secret
        response = self.client.put("/api/settings/config", headers=headers, json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "SETTINGS_VALIDATION_FAILED")
        self.assertNotIn(secret, response.text)
        self.assertIn("llm.enabled", response.json()["error"]["fields"])
        self.assert_safe_headers(response)

    def test_validation_error_fields_only_use_route_schema_whitelists(self) -> None:
        attack_key = "attacker-controlled-field-name"
        attack_value = "DO-NOT-ECHO-ATTACK-VALUE"
        setup = self.client.post(
            "/api/settings/setup",
            headers=self._origin(),
            json={
                "password": "long-enough-password",
                attack_key: attack_value,
            },
        )
        self.assertEqual(setup.status_code, 422)
        self.assertEqual(set(setup.json()["error"]["fields"]), {"request"})
        self.assertNotIn(attack_key, setup.text)
        self.assertNotIn(attack_value, setup.text)

        headers = self._authenticated()
        payload = draft_payload()
        payload["llm"][attack_key] = attack_value
        config = self.client.put(
            "/api/settings/config", headers=headers, json=payload
        )
        self.assertEqual(config.status_code, 422)
        self.assertEqual(set(config.json()["error"]["fields"]), {"llm"})
        self.assertNotIn(attack_key, config.text)
        self.assertNotIn(attack_value, config.text)

        probe = self.client.post(
            "/api/settings/test/qq",
            headers=headers,
            json={"enabled": False, attack_key: attack_value},
        )
        self.assertEqual(probe.status_code, 422)
        self.assertEqual(set(probe.json()["error"]["fields"]), {"request"})
        self.assertNotIn(attack_key, probe.text)
        self.assertNotIn(attack_value, probe.text)

        indexed = draft_payload()
        indexed["qq"]["allowedGroupIds"] = [attack_value]
        response = self.client.put(
            "/api/settings/config", headers=headers, json=indexed
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            set(response.json()["error"]["fields"]),
            {"qq.allowedGroupIds"},
        )
        self.assertNotIn(".0", response.text)
        self.assertNotIn(attack_value, response.text)

    def test_domain_errors_map_without_private_context(self) -> None:
        headers = self._authenticated()
        cases = (
            (SettingsValidationError({"llm.model": "配置值无效"}), 422, "SETTINGS_VALIDATION_FAILED"),
            (SettingsServiceError("SETTINGS_CONFLICT"), 409, "SETTINGS_CONFLICT"),
            (SettingsServiceError("KEYCHAIN_UNAVAILABLE"), 503, "KEYCHAIN_UNAVAILABLE"),
        )
        for error, status, code in cases:
            self.service.save = Mock(side_effect=error)
            response = self.client.put(
                "/api/settings/config", headers=headers, json=draft_payload()
            )
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("cause", response.text)

    def test_unexpected_settings_failure_is_sanitized_with_security_headers(self) -> None:
        self._authenticated()
        self.service.get_config = Mock(
            side_effect=RuntimeError("private path and private-token")
        )

        response = self.client.get("/api/settings/config")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json()["error"]["code"], "SETTINGS_INTERNAL_ERROR"
        )
        self.assertNotIn("private", response.text)
        self.assert_safe_headers(response)

    def test_rate_limit_and_password_policy_have_stable_codes(self) -> None:
        for error, status, code in (
            (LoginRateLimited("private"), 429, "SETTINGS_RATE_LIMITED"),
            (PasswordPolicyError("private"), 422, "SETTINGS_PASSWORD_INVALID"),
        ):
            self.service.login = Mock(side_effect=error)
            response = self.client.post(
                "/api/settings/login",
                headers=self._origin(),
                json={"password": "DO-NOT-ECHO"},
            )
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("private", response.text)

    def test_security_headers_cover_success_errors_head_and_options(self) -> None:
        self.assert_safe_headers(self.client.get("/api/settings/session"))
        self.assert_safe_headers(self.client.get("/api/settings/config"))
        head = self.client.head("/api/settings/session")
        self.assert_safe_headers(head)
        options = self.client.options("/api/settings/config", headers=self._origin())
        self.assert_safe_headers(options)
        self.assertNotIn("access-control-allow-origin", options.headers)


class SettingsApiFactoryTests(unittest.TestCase):
    def test_create_app_does_not_construct_default_settings_service(self) -> None:
        with patch("api.app.create_settings_service") as factory:
            application = create_app(runtime_instance=Mock())
        factory.assert_not_called()
        self.assertIsNone(application.state.settings_service)

    def test_lazy_factory_constructs_once_under_concurrent_first_access(self) -> None:
        service = FakeSettingsService()
        service.initialized = True
        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            return service

        app = create_app(runtime_instance=Mock(), settings_service_factory=factory)

        def request_once(_):
            client = TestClient(
                app, base_url=BASE_URL, client=("127.0.0.1", 50000)
            )
            try:
                return client.get("/api/settings/session").status_code
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(request_once, range(16)))
        self.assertEqual(statuses, [200] * 16)
        self.assertEqual(calls, 1)

    def test_real_service_authorization_fails_closed_without_exposing_session(self) -> None:
        from settings.service import SettingsService

        auth = Mock()
        session = Session("private-token", "private-csrf", 2000.0)
        auth.get_session.return_value = session
        auth.validate_csrf.return_value = True
        service = SettingsService.__new__(SettingsService)
        service._auth = auth

        self.assertEqual(service.authorize("private-token"), (True, True))
        self.assertEqual(
            service.authorize("private-token", "private-csrf", require_csrf=True),
            (True, True),
        )
        auth.validate_csrf.side_effect = RuntimeError("private-csrf")
        self.assertEqual(
            service.authorize("private-token", "candidate", require_csrf=True),
            (True, False),
        )
        auth.get_session.side_effect = RuntimeError("private-token")
        self.assertEqual(service.authorize("private-token"), (False, False))


if __name__ == "__main__":
    unittest.main()
