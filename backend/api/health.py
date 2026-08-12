"""Minimal, redacted health checks for local and cloud operation."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.dependencies import get_runtime
from channels.onebot.models import QQState
from core.runtime import AssistantRuntime


router = APIRouter(tags=["health"])


@router.get("/health/live")
def get_live_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=None)
def get_ready_health(
    runtime: AssistantRuntime = Depends(get_runtime),
):
    try:
        runtime.store.schema_version
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready"},
        )
    return {"status": "ready"}


@router.get("/health/onebot")
def get_onebot_health(
    runtime: AssistantRuntime = Depends(get_runtime),
) -> dict[str, str]:
    settings = runtime.qq_settings
    if settings.configuration_error is not None:
        state = QQState.MISCONFIGURED
    elif not settings.enabled:
        state = QQState.DISABLED
    elif runtime.qq_connection.connected:
        state = QQState.CONNECTED
    else:
        state = QQState.DISCONNECTED
    return {"status": state.value}
