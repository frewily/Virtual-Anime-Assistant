from fastapi import Request

from core.runtime import AssistantRuntime


def get_runtime(request: Request) -> AssistantRuntime:
    return request.app.state.runtime
