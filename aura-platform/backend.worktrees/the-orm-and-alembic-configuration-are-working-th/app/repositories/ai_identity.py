"""AI Identity repository for database access to AI identity records."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.ai_identity import AIIdentity
from app.repositories.base import BaseRepository


class AIIdentityRepository(BaseRepository[AIIdentity]):
    """Repository for AIIdentity model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize AIIdentityRepository with async session."""
        super().__init__(session, AIIdentity)

    async def get_by_user_id(self, user_id: uuid.UUID) -> AIIdentity | None:
        """Retrieve the AI identity for a specific user."""
        result = await self.session.execute(
            select(AIIdentity).where(AIIdentity.user_id == user_id)
        )
        return result.scalars().first()

    async def get_by_ai_uuid(self, ai_uuid: uuid.UUID) -> AIIdentity | None:
        """Retrieve an AI identity by its ai_uuid."""
        result = await self.session.execute(
            select(AIIdentity).where(AIIdentity.ai_uuid == ai_uuid)
        )
        return result.scalars().first()

    async def get_active_identities(self, limit: int = 100) -> list[AIIdentity]:
        """Retrieve all active AI identities."""
        result = await self.session.execute(
            select(AIIdentity)
            .where(AIIdentity.status == "active")
            .limit(limit)
        )
        return result.scalars().all()
