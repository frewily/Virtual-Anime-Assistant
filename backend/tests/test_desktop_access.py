from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.desktop_access import (
    DESKTOP_TOKEN_COOKIE,
    DesktopAccessMiddleware,
    desktop_websocket_subprotocol,
    normalize_desktop_access_token,
)
from settings.security import SettingsSecurityMiddleware


TOKEN = "a" * 43


def make_client(token: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/api/private")
    def private_route():
        return {"ok": True}

    @app.get("/settings")
    def settings_page():
        return {"page": True}

    @app.get("/api/settings/session")
    def settings_session():
        return {"authenticated": False}

    app.add_middleware(DesktopAccessMiddleware, token=token)
    return TestClient(app)


class DesktopAccessMiddlewareTests(unittest.TestCase):
    def test_disabled_mode_preserves_development_api(self) -> None:
        response = make_client(None).get("/api/private")
        self.assertEqual(response.status_code, 200)

    def test_protected_api_rejects_missing_wrong_and_duplicate_headers(self) -> None:
        client = make_client(TOKEN)
        self.assertEqual(client.get("/api/private").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/private", headers={"X-VAA-Desktop-Token": "b" * 43}
            ).status_code,
            401,
        )
        self.assertEqual(
            client.get(
                "/api/private",
                headers=[
                    ("X-VAA-Desktop-Token", TOKEN),
                    ("X-VAA-Desktop-Token", TOKEN),
                ],
            ).status_code,
            401,
        )

    def test_header_bootstraps_httponly_session_cookie(self) -> None:
        client = make_client(TOKEN)
        response = client.get(
            "/api/settings/session", headers={"X-VAA-Desktop-Token": TOKEN}
        )
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{DESKTOP_TOKEN_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(client.get("/api/settings/session").status_code, 200)
        self.assertEqual(client.get("/api/private").status_code, 401)

    def test_static_settings_page_and_preflight_do_not_require_token(self) -> None:
        client = make_client(TOKEN)
        self.assertEqual(client.get("/settings").status_code, 200)
        self.assertNotEqual(client.options("/api/private").status_code, 401)

    def test_websocket_subprotocol_requires_one_exact_capability(self) -> None:
        expected = f"vaa.desktop.{TOKEN}"
        scope = {
            "headers": [(b"sec-websocket-protocol", expected.encode("ascii"))]
        }
        self.assertEqual(desktop_websocket_subprotocol(scope, TOKEN), expected)
        self.assertIsNone(desktop_websocket_subprotocol({"headers": []}, TOKEN))
        self.assertIsNone(
            desktop_websocket_subprotocol(
                {
                    "headers": [
                        (
                            b"sec-websocket-protocol",
                            f"{expected}, attacker".encode("ascii"),
                        )
                    ]
                },
                TOKEN,
            )
        )

    def test_token_format_is_fail_closed(self) -> None:
        self.assertEqual(normalize_desktop_access_token(TOKEN), TOKEN)
        self.assertIsNone(normalize_desktop_access_token(None))
        with self.assertRaises(ValueError):
            normalize_desktop_access_token("short")

    def test_settings_transport_uses_the_runtime_port(self) -> None:
        app = FastAPI()

        @app.get("/settings")
        def settings_page():
            return {"ok": True}

        app.add_middleware(SettingsSecurityMiddleware)
        with patch.dict("os.environ", {"ASSISTANT_PORT": "49152"}):
            client = TestClient(
                app,
                base_url="http://127.0.0.1:49152",
                client=("127.0.0.1", 50000),
            )
            self.assertEqual(client.get("/settings").status_code, 200)
            self.assertEqual(
                client.get(
                    "/settings", headers={"Host": "127.0.0.1:8080"}
                ).status_code,
                403,
            )


if __name__ == "__main__":
    unittest.main()
