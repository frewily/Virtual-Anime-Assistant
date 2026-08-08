"""HTTP contract for the local settings interface."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import ConfigDict, SecretStr
from starlette.responses import JSONResponse
from starlette.responses import FileResponse

from api.dependencies import get_runtime, get_settings_service
from api.qq import get_qq_status
from core.runtime import AssistantRuntime
from settings.models import RequestModel
from settings.security import SETTINGS_SESSION_COOKIE
from settings.service import (
    LLMProbeDraft,
    QQProbeDraft,
    SettingsService,
    VersionedSettingsDraft,
)
from settings.validation import TTSTestRequest


router = APIRouter(prefix="/settings", tags=["settings"])
static_router = APIRouter(tags=["settings-page"])
_STATIC_DIRECTORY = Path(__file__).with_name("static")
_STATIC_ASSETS = {
    "settings.css": "text/css",
    "settings.js": "text/javascript",
}


@static_router.get("/settings", include_in_schema=False)
@static_router.get("/settings/", include_in_schema=False)
@static_router.head("/settings", include_in_schema=False)
@static_router.head("/settings/", include_in_schema=False)
def settings_page():
    return FileResponse(_STATIC_DIRECTORY / "index.html", media_type="text/html")


@static_router.get("/settings/{asset_name}", include_in_schema=False)
@static_router.head("/settings/{asset_name}", include_in_schema=False)
def settings_asset(asset_name: str):
    media_type = _STATIC_ASSETS.get(asset_name)
    if media_type is None:
        raise SettingsHttpError(404, "SETTINGS_ASSET_NOT_FOUND", "页面资源不存在")
    return FileResponse(_STATIC_DIRECTORY / asset_name, media_type=media_type)


class _StrictRequest(RequestModel):
    model_config = ConfigDict(**RequestModel.model_config, strict=True)


class PasswordRequest(_StrictRequest):
    password: SecretStr


def _response(content: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content, by_alias=True),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SETTINGS_SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        path="/",
        secure=False,
    )


def _single_request_header(request: Request, name: bytes) -> str | None:
    values = [
        value
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _require_auth(
    request: Request,
    service: SettingsService = Depends(get_settings_service),
) -> SettingsService:
    token = request.cookies.get(SETTINGS_SESSION_COOKIE)
    authenticated, _ = service.authorize(token)
    if not authenticated:
        raise SettingsHttpError(401, "SETTINGS_UNAUTHORIZED", "请先登录")
    return service


def _require_write_auth(
    request: Request,
    service: SettingsService = Depends(get_settings_service),
) -> SettingsService:
    token = request.cookies.get(SETTINGS_SESSION_COOKIE)
    csrf = _single_request_header(request, b"x-csrf-token")
    authenticated, csrf_valid = service.authorize(
        token, csrf, require_csrf=True
    )
    if not authenticated:
        raise SettingsHttpError(401, "SETTINGS_UNAUTHORIZED", "请先登录")
    if not csrf_valid:
        raise SettingsHttpError(403, "SETTINGS_CSRF_REJECTED", "请求验证失败")
    return service


class SettingsHttpError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(code)


@router.get("/session")
def session_status(
    request: Request,
    service: SettingsService = Depends(get_settings_service),
):
    return service.session_status(request.cookies.get(SETTINGS_SESSION_COOKIE))


@router.post("/setup")
def setup(
    body: PasswordRequest,
    service: SettingsService = Depends(get_settings_service),
):
    session = service.setup(body.password.get_secret_value())
    response = _response(service.session_status(session.token))
    _set_session_cookie(response, session.token)
    return response


@router.post("/login")
def login(
    body: PasswordRequest,
    request: Request,
    service: SettingsService = Depends(get_settings_service),
):
    client = request.client.host if request.client else ""
    session = service.login(client, body.password.get_secret_value())
    if session is None:
        raise SettingsHttpError(401, "SETTINGS_AUTH_FAILED", "密码错误")
    response = _response(service.session_status(session.token))
    _set_session_cookie(response, session.token)
    return response


@router.post("/logout")
def logout(
    request: Request,
    service: SettingsService = Depends(_require_write_auth),
):
    service.logout(request.cookies.get(SETTINGS_SESSION_COOKIE))
    response = _response({"ok": True})
    response.delete_cookie(
        SETTINGS_SESSION_COOKIE,
        path="/",
        secure=False,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/config")
def get_config(service: SettingsService = Depends(_require_auth)):
    return service.get_config_snapshot()


@router.put("/config")
def save_config(
    draft: VersionedSettingsDraft,
    service: SettingsService = Depends(_require_write_auth),
):
    return service.save_with_snapshot(draft)


@router.get("/voices")
def get_voices(service: SettingsService = Depends(_require_auth)):
    return service.get_voices()


@router.post("/test/llm")
async def test_llm(
    body: LLMProbeDraft,
    service: SettingsService = Depends(_require_write_auth),
):
    return await service.test_llm(body)


@router.post("/test/qq")
async def test_qq(
    body: QQProbeDraft,
    service: SettingsService = Depends(_require_write_auth),
    runtime: AssistantRuntime = Depends(get_runtime),
):
    try:
        status = get_qq_status(runtime)
    except Exception:
        status = {
            "enabled": False,
            "state": "disabled",
            "allowedGroupCount": 0,
            "allowedUserCount": 0,
        }
    return await service.test_qq(body, status)


@router.post("/test/tts")
async def test_tts(
    body: TTSTestRequest,
    service: SettingsService = Depends(_require_write_auth),
):
    return await service.test_tts(body)


__all__ = ["SettingsHttpError", "router", "static_router"]
