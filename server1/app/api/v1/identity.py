"""Identity API routes for AI identity and personal brain management."""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.user import User
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AIIdentityResponse:
    """Create a new AI identity for the authenticated user."""
    if current_user.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to create an identity for another user")

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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AIIdentityResponse:
    """Retrieve an AI identity by ID for the authenticated user."""
    service = AIIdentityService(session)
    identity = await service.get_identity_by_id(identity_id)
    if not identity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AI identity not found",
        )
    if identity.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's identity")
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PersonalAIBrainResponse:
    """Create a personal AI brain for the authenticated user's AI identity."""
    service = AIIdentityService(session)
    identity = await service.get_identity_by_id(ai_identity_id)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI identity not found")
    if identity.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to create a brain for another user's identity")

    personal_brain_service = PersonalBrainService(session)
    brain = await personal_brain_service.create_brain(
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
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PersonalAIBrainResponse:
    """Retrieve the authenticated user's personal brain."""
    service = PersonalBrainService(session)
    brain = await service.get_brain_by_ai_identity(ai_identity_id)
    if not brain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal brain not found",
        )
    identity = await AIIdentityService(session).get_identity_by_id(ai_identity_id)
    if identity is None or identity.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's brain")
    return brain


@router.get(
    "/brain/{ai_identity_id}/context",
    response_model=BrainContextResponse,
    tags=["Brain"],
)
async def get_brain_context(
    ai_identity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> BrainContextResponse:
    """Retrieve aggregated context for the authenticated user's personal brain."""
    service = PersonalBrainService(session)
    brain = await service.get_brain_by_ai_identity(ai_identity_id)
    if brain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personal brain not found")
    identity = await AIIdentityService(session).get_identity_by_id(ai_identity_id)
    if identity is None or identity.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to access another user's brain context")
    context = await service.get_brain_context(ai_identity_id)
    return context
