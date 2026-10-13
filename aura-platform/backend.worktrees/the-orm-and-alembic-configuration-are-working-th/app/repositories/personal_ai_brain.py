"""Personal AI Brain repository for database access to brain records."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.personal_ai_brain import PersonalAIBrain
from app.repositories.base import BaseRepository


class PersonalAIBrainRepository(BaseRepository[PersonalAIBrain]):
    """Repository for PersonalAIBrain model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize PersonalAIBrainRepository with async session."""
        super().__init__(session, PersonalAIBrain)

    async def get_by_ai_identity_id(self, ai_identity_id: uuid.UUID) -> PersonalAIBrain | None:
        """Retrieve the personal AI brain for a specific AI identity."""
        result = await self.session.execute(
            select(PersonalAIBrain).where(
                PersonalAIBrain.ai_identity_id == ai_identity_id
            )
        )
        return result.scalars().first()

    async def get_active_brains(self, limit: int = 100) -> list[PersonalAIBrain]:
        """Retrieve all active personal AI brains."""
        result = await self.session.execute(
            select(PersonalAIBrain)
            .where(PersonalAIBrain.status == "active")
            .limit(limit)
        )
        return result.scalars().all()
