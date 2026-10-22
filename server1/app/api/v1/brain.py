"""Personal Brain API boundary for app-to-brain communication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.infrastructure.database.session import get_db
from app.models.brain_memory import MemoryKind
from app.models.user import User
from app.schemas.brain import (
    BrainIngestRequest,
    BrainIngestResponse,
    BrainProvisioningResponse,
    BrainQueryRequest,
    BrainQueryResponse,
    MemoryCreateRequest,
    MemoryResponse,
    MemoryUpdateRequest,
)
from app.services.app_registry import AppConnectionService
from app.services.personal_brain import BrainRecordRetrievalError, PersonalBrainService
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository

router = APIRouter(prefix="/brain", tags=["Brain"])


@router.get("/me", response_model=BrainProvisioningResponse)
async def my_brain(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainProvisioningResponse:
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if identity is None or brain is None:
        raise HTTPException(status_code=404, detail="Personal Brain is not provisioned")
    return BrainProvisioningResponse(
        brain_id=brain.id,
        ai_identity_id=identity.id,
        provisioning_status=brain.provisioning_status,
        resource_manifest_version=brain.resource_manifest_version,
        model_configuration=brain.model_configuration,
        inference_runtime_status=brain.inference_runtime_status,
        compute_allocation=brain.compute_allocation,
        storage_resource_reference=brain.storage_resource_reference,
        last_health_check=brain.last_health_check,
        provisioning_error=brain.provisioning_error,
        llm_runtime_id=brain.llm_runtime_id,
        runtime_isolation=brain.runtime_isolation,
    )


@router.post("/me/reconcile", response_model=BrainProvisioningResponse)
async def reconcile_my_brain(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainProvisioningResponse:
    """Retry idempotent Server 2 registration for this authenticated owner."""
    from app.api.v1.users import _provision_primary_brain

    await _provision_primary_brain(session, current_user)
    return await my_brain(current_user=current_user, session=session)


@router.post("/query", response_model=BrainQueryResponse)
async def query_brain(
    payload: BrainQueryRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainQueryResponse:
    """Process a user query through their Personal Brain."""
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if payload.source_app:
        authorized = await AppConnectionService(session).has_active_permission(
            current_user, payload.source_app, scope="brain", action="read"
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="App is not authorized to query this user's brain",
            )
    if (
        get_settings().brain_remote_provisioning_enabled
        and (brain is None or brain.provisioning_status != "ready")
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The user's Brain is not ready; retry provisioning before querying.",
        )

    service = PersonalBrainService(session)
    try:
        result = await service.process_query(
            current_user,
            payload.query,
            payload.context or {},
            use_memory=payload.use_memory,
            persist_interaction=payload.persist_interaction,
            memory_limit=payload.memory_limit,
        )
    except BrainRecordRetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A selected Brain record is temporarily unavailable.",
        ) from exc
    return BrainQueryResponse(**result)


@router.post("/ingest", response_model=BrainIngestResponse)
async def ingest_to_brain(
    payload: BrainIngestRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainIngestResponse:
    """Send application-generated data to the user's Personal Brain for classification."""
    if payload.source_app:
        authorized = await AppConnectionService(session).has_active_permission(
            current_user, payload.source_app, scope="memory", action="write"
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="App is not authorized to ingest into this user's brain",
            )

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

@router.get("/memory/search", response_model=list[MemoryResponse])
async def search_memories(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[MemoryResponse]:
    """Search persisted memories for the authenticated user."""
    service = PersonalBrainService(session)
    memories = await service.search_memories_for_user(current_user, query=q, limit=limit)
    return [MemoryResponse.model_validate(memory) for memory in memories]


@router.get("/memory/{memory_id}", response_model=MemoryResponse)
async def get_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Get a specific memory for the authenticated user."""
    service = PersonalBrainService(session)
    memory = await service.get_memory_for_user(current_user, memory_id)
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return MemoryResponse.model_validate(memory)


@router.put("/memory/{memory_id}", response_model=MemoryResponse)
@router.patch("/memory/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MemoryResponse:
    """Update a memory for the authenticated user."""
    service = PersonalBrainService(session)
    memory = await service.update_memory_for_user(
        current_user,
        memory_id,
        content=payload.content,
        summary=payload.summary,
        importance=payload.importance,
    )
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return MemoryResponse.model_validate(memory)


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Delete a memory for the authenticated user."""
    service = PersonalBrainService(session)
    result = await service.delete_memory_for_user(current_user, memory_id)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return None
