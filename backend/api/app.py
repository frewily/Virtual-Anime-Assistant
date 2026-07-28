import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from api.avatar import router as avatar_router
from api.chat import router as chat_router
from api.conversations import router as conversations_router
from api.memories import router as memories_router
from api.status import router as status_router
from api.tts import router as tts_router
from api.window import router as window_router
from api.ws import router as ws_router
from api.ws import broadcast_to_desktop
from agent.monitor import run as run_window_monitor
from core.runtime import AssistantRuntime
from core.tts import AUDIO_DIR


logger = logging.getLogger(__name__)


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
        runtime = AssistantRuntime()
        app.state.runtime = runtime
    unsubscribe = None
    tasks: list[asyncio.Task] = []
    try:
        unsubscribe = runtime.application.publisher.subscribe(
            broadcast_to_desktop
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
            await runtime.aclose()


def create_app(runtime_instance: AssistantRuntime | None = None) -> FastAPI:
    app = FastAPI(title="Desktop Assistant API", version="1.0.0", lifespan=lifespan)
    app.state.runtime = runtime_instance

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
    app.include_router(ws_router)
    app.mount(
        "/api/tts/audio",
        StaticFiles(directory=AUDIO_DIR, check_dir=False),
        name="tts-audio",
    )

    return app


app = create_app()
