"""Knowledge Graph repository for database access to knowledge graph records."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.knowledge_graph import KnowledgeGraph
from app.repositories.base import BaseRepository


class KnowledgeGraphRepository(BaseRepository[KnowledgeGraph]):
    """Repository for KnowledgeGraph model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize KnowledgeGraphRepository with async session."""
        super().__init__(session, KnowledgeGraph)

    async def get_by_ai_identity_id(self, ai_identity_id: uuid.UUID) -> KnowledgeGraph | None:
        """Retrieve the knowledge graph for a specific AI identity."""
        result = await self.session.execute(
            select(KnowledgeGraph).where(
                KnowledgeGraph.ai_identity_id == ai_identity_id
            )
        )
        return result.scalars().first()

    async def get_by_graph_type(
        self, ai_identity_id: uuid.UUID, graph_type: str
    ) -> KnowledgeGraph | None:
        """Retrieve a knowledge graph by type for an AI identity."""
        result = await self.session.execute(
            select(KnowledgeGraph).where(
                (KnowledgeGraph.ai_identity_id == ai_identity_id)
                & (KnowledgeGraph.graph_type == graph_type)
            )
        )
        return result.scalars().first()

    async def get_active_graphs(self, limit: int = 100) -> list[KnowledgeGraph]:
        """Retrieve all active knowledge graphs."""
        result = await self.session.execute(
            select(KnowledgeGraph)
            .where(KnowledgeGraph.status == "active")
            .limit(limit)
        )
        return result.scalars().all()
