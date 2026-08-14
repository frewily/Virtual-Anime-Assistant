from fastapi import APIRouter, Depends, Request

from api.dependencies import get_runtime
from core.cloud_operations import CloudOperationsReader, CloudOperationsSnapshot
from core.runtime import AssistantRuntime

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status(runtime: AssistantRuntime = Depends(get_runtime)):
    return runtime.status()


@router.get("/status/cloud")
def get_cloud_status(request: Request) -> CloudOperationsSnapshot:
    deployment = request.app.state.deployment_settings
    return CloudOperationsReader.from_deployment(deployment).snapshot()
