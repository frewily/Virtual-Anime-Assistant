from fastapi import APIRouter
from core.runtime import runtime

router = APIRouter(tags=["status"])
@router.get("/status")
def get_status():
    return runtime.status()
