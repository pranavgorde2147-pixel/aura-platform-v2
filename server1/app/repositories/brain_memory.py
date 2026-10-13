"""Brain Memory repository for database access to personal memories."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brain_memory import BrainMemory
from app.repositories.base import BaseRepository


class BrainMemoryRepository(BaseRepository[BrainMemory]):
    """Repository for BrainMemory model operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(session, BrainMemory)

    async def get_recent_by_identity(
        self, ai_identity_id: uuid.UUID, limit: int = 20
    ) -> list[BrainMemory]:
        """Return the most recent memories for an identity."""
        result = await self.session.execute(
            select(BrainMemory)
            .where(BrainMemory.ai_identity_id == ai_identity_id)
            .order_by(BrainMemory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def search_by_identity(
        self, ai_identity_id: uuid.UUID, query: str, limit: int = 10
    ) -> list[BrainMemory]:
        """Return memories for an identity matching a query by keyword.

        This is a deterministic, dependency-free retrieval fallback. When a
        vector store is configured, semantic retrieval can be layered on top
        without changing the API contract.
        """
        like = f"%{query.strip()}%"
        result = await self.session.execute(
            select(BrainMemory)
            .where(
                BrainMemory.ai_identity_id == ai_identity_id,
                or_(BrainMemory.content.ilike(like), BrainMemory.summary.ilike(like)),
            )
            .order_by(BrainMemory.importance.desc(), BrainMemory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_identity(
        self, ai_identity_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[BrainMemory]:
        """Return paginated memories for an identity."""
        result = await self.session.execute(
            select(BrainMemory)
            .where(BrainMemory.ai_identity_id == ai_identity_id)
            .order_by(BrainMemory.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())
