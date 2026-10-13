"""Personal Brain API boundary for app-to-brain communication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.brain_memory import MemoryKind
from app.models.user import User
from app.schemas.brain import (
    BrainIngestRequest,
    BrainIngestResponse,
    BrainQueryRequest,
    BrainQueryResponse,
    MemoryCreateRequest,
    MemoryResponse,
)
from app.services.personal_brain import PersonalBrainService

router = APIRouter(prefix="/brain", tags=["Brain"])


@router.post("/query", response_model=BrainQueryResponse)
async def query_brain(
    payload: BrainQueryRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainQueryResponse:
    """Process a user query through their Personal Brain."""
    service = PersonalBrainService(session)
    result = await service.process_query(
        current_user,
        payload.query,
        payload.context or {},
        use_memory=payload.use_memory,
        persist_interaction=payload.persist_interaction,
        memory_limit=payload.memory_limit,
    )
    return BrainQueryResponse(**result)


@router.post("/ingest", response_model=BrainIngestResponse)
async def ingest_to_brain(
    payload: BrainIngestRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainIngestResponse:
    """Send application-generated data to the user's Personal Brain for classification."""
    service = PersonalBrainService(session)
    result = await service.ingest_data(current_user, payload.model_dump())
    return BrainIngestResponse(**result)


@router.post("/memory", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Explicitly persist a memory in the authenticated user's Personal Brain."""
    service = PersonalBrainService(session)
    memory = await service.store_memory_for_user(
        current_user,
        content=payload.content,
        kind=MemoryKind(payload.kind),
        summary=payload.summary,
        source_app=payload.source_app,
        importance=payload.importance,
    )
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personal brain not found")
    return MemoryResponse.model_validate(memory)


@router.get("/memory", response_model=list[MemoryResponse])
async def list_memories(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[MemoryResponse]:
    """List persisted memories for the authenticated user."""
    service = PersonalBrainService(session)
    memories = await service.list_memories_for_user(current_user, limit=limit, offset=offset)
    return [MemoryResponse.model_validate(memory) for memory in memories]


@router.get("/context")
async def brain_context(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Return the current user's brain context in a protected endpoint."""
    service = PersonalBrainService(session)
    brain = await service.get_brain_for_user(current_user.id)
    if brain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personal brain not found")
    return await service.get_brain_context(brain.ai_identity_id)
