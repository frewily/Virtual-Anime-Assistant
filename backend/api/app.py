import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from api.avatar import router as avatar_router
from api.chat import router as chat_router
from api.conversations import router as conversations_router
from api.memories import router as memories_router
from api.qq import status_router as qq_status_router
from api.qq import websocket_router as qq_websocket_router
from api.status import router as status_router
from api.tools import (
    confirmation_payload,
    request_payload,
    router as tools_router,
)
from api.tts import router as tts_router
from api.window import router as window_router
from api.ws import router as ws_router
from api.ws import broadcast_json, broadcast_to_desktop
from agent.monitor import run as run_window_monitor
from core.runtime import AssistantRuntime
from core.tts import AUDIO_DIR
from domain.tools import ToolEvent
from settings.auth import LoginRateLimited, PasswordPolicyError
from settings.resolver import RuntimeSettings
from settings.routes import SettingsHttpError, router as settings_router
from settings.security import SettingsSecurityMiddleware
from settings.service import (
    SettingsService,
    SettingsServiceError,
    create_settings_service,
)
from settings.validation import SettingsValidationError


logger = logging.getLogger(__name__)


async def broadcast_tool_event(event: ToolEvent) -> None:
    payload = {
        "type": event.type.value,
        "request": request_payload(event.request),
    }
    if event.request.confirmation is not None:
        payload["confirmation"] = confirmation_payload(
            event.request.confirmation
        )
    await broadcast_json(payload)


async def scenario_loop(runtime: AssistantRuntime) -> None:
    while True:
        await runtime.check_scenarios()
        await asyncio.sleep(10)


async def supervise(name: str, task_factory) -> None:
    while True:
        try:
            await task_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task failed: %s", name)
            await asyncio.sleep(5)


def _start_background_task(coroutine, tasks: list[asyncio.Task]) -> None:
    try:
        task = asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise
    tasks.append(task)


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = app.state.runtime
    if runtime is None:
        runtime_settings_factory = app.state.runtime_settings_factory
        if runtime_settings_factory is None:
            runtime = AssistantRuntime()
        else:
            runtime = AssistantRuntime(
                runtime_settings=runtime_settings_factory()
            )
        app.state.runtime = runtime
    unsubscribe = None
    unsubscribe_tools = None
    tasks: list[asyncio.Task] = []
    try:
        unsubscribe = runtime.application.publisher.subscribe(
            broadcast_to_desktop
        )
        if runtime.tool_service is not None:
            unsubscribe_tools = runtime.tool_service.publisher.subscribe(
                broadcast_tool_event
            )
        _start_background_task(
            supervise("scenario-loop", lambda: scenario_loop(runtime)),
            tasks,
        )
        _start_background_task(
            supervise(
                "window-monitor",
                lambda: run_window_monitor(runtime.report_window),
            ),
            tasks,
        )
        yield
    finally:
        for task in tasks:
            try:
                task.cancel()
            except Exception:
                logger.exception("Failed to cancel background task")
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            if unsubscribe is not None:
                unsubscribe()
        finally:
            try:
                if unsubscribe_tools is not None:
                    unsubscribe_tools()
            finally:
                await runtime.aclose()


def create_app(
    runtime_instance: AssistantRuntime | None = None,
    runtime_settings_factory: Callable[[], RuntimeSettings] | None = None,
    settings_service: SettingsService | None = None,
    settings_service_factory: Callable[
        [], SettingsService
    ] = create_settings_service,
) -> FastAPI:
    app = FastAPI(title="Desktop Assistant API", version="1.0.0", lifespan=lifespan)
    app.state.runtime = runtime_instance
    app.state.runtime_settings_factory = runtime_settings_factory
    app.state.settings_service = settings_service
    app.state.settings_service_factory = settings_service_factory
    app.state.settings_service_lock = threading.Lock()

    @app.exception_handler(SettingsHttpError)
    async def handle_settings_http_error(request: Request, exc: SettingsHttpError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(SettingsValidationError)
    async def handle_settings_validation_error(
        request: Request, exc: SettingsValidationError
    ):
        return JSONResponse(status_code=422, content=exc.to_dict())

    @app.exception_handler(SettingsServiceError)
    async def handle_settings_service_error(
        request: Request, exc: SettingsServiceError
    ):
        status = 409 if exc.code == "SETTINGS_CONFLICT" else 503
        if exc.code == "SETTINGS_ALREADY_INITIALIZED":
            status = 409
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.exception_handler(LoginRateLimited)
    async def handle_settings_rate_limit(request: Request, exc: LoginRateLimited):
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "SETTINGS_RATE_LIMITED",
                    "message": "登录尝试过多，请稍后重试",
                }
            },
        )

    @app.exception_handler(PasswordPolicyError)
    async def handle_settings_password_policy(
        request: Request, exc: PasswordPolicyError
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "SETTINGS_PASSWORD_INVALID",
                    "message": "密码长度必须为 10 到 128 个字符",
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/api/settings"):
            return await request_validation_exception_handler(request, exc)
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            fields[".".join(location) or "request"] = "配置值无效"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "SETTINGS_VALIDATION_FAILED",
                    "message": "请检查标记的配置项",
                    "fields": fields,
                }
            },
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["null", "file://"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    app.include_router(status_router, prefix="/api")
    app.include_router(tts_router, prefix="/api")
    app.include_router(window_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(avatar_router, prefix="/api")
    app.include_router(memories_router, prefix="/api")
    app.include_router(conversations_router, prefix="/api")
    app.include_router(tools_router, prefix="/api")
    app.include_router(qq_status_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")
    app.include_router(ws_router)
    app.include_router(qq_websocket_router)
    app.mount(
        "/api/tts/audio",
        StaticFiles(directory=AUDIO_DIR, check_dir=False),
        name="tts-audio",
    )

    app.add_middleware(SettingsSecurityMiddleware)

    return app


app = create_app()
