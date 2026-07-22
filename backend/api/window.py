from fastapi import APIRouter
from core.runtime import runtime

router = APIRouter(tags=["window"])

@router.post("/report/window")
def report_window(window: dict):
    runtime.report_window(window)
    return {"status": "ok"}
