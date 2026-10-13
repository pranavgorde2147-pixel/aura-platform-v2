"""Identity API routes for AI identity and personal brain management."""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import get_db
from app.services.ai_identity import AIIdentityService
from app.services.personal_brain import PersonalBrainService
from app.schemas.identity import (
    AIIdentityResponse,
    AIIdentityCreate,
    PersonalAIBrainResponse,
    PersonalAIBrainCreate,
    BrainContextResponse,
)
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "/identity",
    response_model=AIIdentityResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Identity"],
)
async def create_ai_identity(
    user_id: uuid.UUID,
    data: AIIdentityCreate,
    session: AsyncSession = Depends(get_db),
) -> AIIdentityResponse:
    """Create a new AI identity for a user."""
    service = AIIdentityService(session)
    identity = await service.create_ai_identity(
        user_id=user_id,
        display_name=data.display_name,
    )
    return identity


@router.get(
    "/identity/{identity_id}",
    response_model=AIIdentityResponse,
    tags=["Identity"],
)
async def get_ai_identity(
    identity_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> AIIdentityResponse:
    """Retrieve an AI identity by ID."""
    service = AIIdentityService(session)
    identity = await service.get_identity_by_id(identity_id)
    if not identity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI identity not found",
        )
    return identity


@router.post(
    "/brain",
    response_model=PersonalAIBrainResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Brain"],
)
async def create_personal_brain(
    ai_identity_id: uuid.UUID,
    data: PersonalAIBrainCreate,
    session: AsyncSession = Depends(get_db),
) -> PersonalAIBrainResponse:
    """Create a personal AI brain for an AI identity."""
    service = PersonalBrainService(session)
    brain = await service.create_brain(
        ai_identity_id=ai_identity_id,
        brain_name=data.brain_name,
        version=data.version,
    )
    return brain


@router.get(
    "/brain/{ai_identity_id}",
    response_model=PersonalAIBrainResponse,
    tags=["Brain"],
)
async def get_personal_brain(
    ai_identity_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> PersonalAIBrainResponse:
    """Retrieve the personal brain for an AI identity."""
    service = PersonalBrainService(session)
    brain = await service.get_brain_by_ai_identity(ai_identity_id)
    if not brain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal brain not found",
        )
    return brain


@router.get(
    "/brain/{ai_identity_id}/context",
    response_model=BrainContextResponse,
    tags=["Brain"],
)
async def get_brain_context(
    ai_identity_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> BrainContextResponse:
    """Retrieve aggregated context for a personal brain."""
    service = PersonalBrainService(session)
    context = await service.get_brain_context(ai_identity_id)
    return context
