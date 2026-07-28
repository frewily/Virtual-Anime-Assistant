from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from api.dependencies import get_runtime
from channels.desktop import LOCAL_USER
from core.runtime import AssistantRuntime
from memory.commands import normalize_memory_content
from memory.models import MemoryItem

router = APIRouter(tags=["memories"])


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content", mode="before")
    @classmethod
    def content_must_not_be_blank(cls, value):
        if not isinstance(value, str):
            return value
        content = value.strip()
        if not content:
            raise ValueError("memory content must not be blank")
        return content


@router.get("/memories")
async def list_memories(
    runtime: AssistantRuntime = Depends(get_runtime),
) -> list[MemoryItem]:
    return await runtime.store.list_memories("desktop", LOCAL_USER.id)


@router.post("/memories", status_code=status.HTTP_201_CREATED)
async def create_memory(
    request: MemoryCreate,
    runtime: AssistantRuntime = Depends(get_runtime),
) -> MemoryItem:
    return await runtime.store.save_memory(
        MemoryItem(
            source="desktop",
            owner_id=LOCAL_USER.id,
            content=request.content,
            normalized_content=normalize_memory_content(request.content),
        )
    )


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str,
    runtime: AssistantRuntime = Depends(get_runtime),
) -> Response:
    deleted = await runtime.store.delete_memory_by_id(
        memory_id,
        "desktop",
        LOCAL_USER.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
