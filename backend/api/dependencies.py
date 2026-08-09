from fastapi import Request

from core.runtime import AssistantRuntime
from settings.service import SettingsService


def get_runtime(request: Request) -> AssistantRuntime:
    return request.app.state.runtime


def get_settings_service(request: Request) -> SettingsService:
    service = request.app.state.settings_service
    if service is not None:
        return service
    with request.app.state.settings_service_lock:
        service = request.app.state.settings_service
        if service is None:
            service = request.app.state.settings_service_factory()
            request.app.state.settings_service = service
        return service
