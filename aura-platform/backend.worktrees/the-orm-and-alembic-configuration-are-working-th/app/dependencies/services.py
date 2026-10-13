"""Dependency injection utilities for FastAPI."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import get_db
from app.services.user import UserService
from app.services.ai_identity import AIIdentityService
from app.services.personal_brain import PersonalBrainService


async def get_user_service(session: AsyncSession = None) -> UserService:
    """Provide a UserService instance."""
    if session is None:
        async for session in get_db():
            return UserService(session)
    return UserService(session)


async def get_ai_identity_service(session: AsyncSession = None) -> AIIdentityService:
    """Provide an AIIdentityService instance."""
    if session is None:
        async for session in get_db():
            return AIIdentityService(session)
    return AIIdentityService(session)


async def get_personal_brain_service(session: AsyncSession = None) -> PersonalBrainService:
    """Provide a PersonalBrainService instance."""
    if session is None:
        async for session in get_db():
            return PersonalBrainService(session)
    return PersonalBrainService(session)
