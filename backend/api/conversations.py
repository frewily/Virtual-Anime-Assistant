from fastapi import APIRouter, Depends, HTTPException, Response, status

from api.dependencies import get_runtime
from channels.desktop import LOCAL_USER
from core.runtime import AssistantRuntime
from memory.models import StoredMessage

router = APIRouter(tags=["conversations"])
_LOCAL_CONVERSATION_ID = f"desktop:{LOCAL_USER.id}"


def _require_local_conversation(conversation_id: str) -> None:
    if conversation_id != _LOCAL_CONVERSATION_ID:
        raise HTTPException(status_code=404, detail="conversation not found")


@router.get("/conversations/{conversation_id}/messages")
async def list_conversation_messages(
    conversation_id: str,
    runtime: AssistantRuntime = Depends(get_runtime),
) -> list[StoredMessage]:
    _require_local_conversation(conversation_id)
    return await runtime.store.list_messages(conversation_id)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: str,
    runtime: AssistantRuntime = Depends(get_runtime),
) -> Response:
    _require_local_conversation(conversation_id)
    if not await runtime.store.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
