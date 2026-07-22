import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from api.status import router as status_router
from api.tts import router as tts_router
from api.window import router as window_router
from api.chat import router as chat_router
from api.avatar import router as avatar_router
from api.ws import router as ws_router
from api.ws import broadcast_to_desktop
from agent.monitor import run as run_window_monitor
from core.runtime import runtime
from core.tts import AUDIO_DIR


logger = logging.getLogger(__name__)


async def scenario_loop() -> None:
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    unsubscribe = runtime.application.publisher.subscribe(broadcast_to_desktop)
    tasks = [
        asyncio.create_task(supervise("scenario-loop", scenario_loop)),
        asyncio.create_task(
            supervise("window-monitor", lambda: run_window_monitor(runtime.report_window))
        ),
    ]
    try:
        yield
    finally:
        unsubscribe()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title="Desktop Assistant API", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["null", "file://"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.include_router(status_router, prefix="/api")
    app.include_router(tts_router, prefix="/api")
    app.include_router(window_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(avatar_router, prefix="/api")
    app.include_router(ws_router)
    app.mount("/api/tts/audio", StaticFiles(directory=AUDIO_DIR), name="tts-audio")

    return app


app = create_app()
