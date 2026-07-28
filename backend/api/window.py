from fastapi import APIRouter, Depends

from api.dependencies import get_runtime
from core.runtime import AssistantRuntime

router = APIRouter(tags=["window"])


@router.post("/report/window")
def report_window(
    window: dict,
    runtime: AssistantRuntime = Depends(get_runtime),
):
    runtime.report_window(window)
    return {"status": "ok"}
