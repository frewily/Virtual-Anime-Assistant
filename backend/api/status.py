from fastapi import APIRouter, Depends

from api.dependencies import get_runtime
from core.runtime import AssistantRuntime

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status(runtime: AssistantRuntime = Depends(get_runtime)):
    return runtime.status()
