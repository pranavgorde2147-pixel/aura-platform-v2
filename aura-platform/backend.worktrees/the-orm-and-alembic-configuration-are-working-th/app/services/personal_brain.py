"""Personal AI Brain service layer for orchestration."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_identity import AIIdentity
from app.models.personal_ai_brain import PersonalAIBrain
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.repositories.memory_vault import MemoryVaultRepository
from app.repositories.knowledge_graph import KnowledgeGraphRepository
from app.repositories.timeline import TimelineRepository
from app.core.logging import get_logger

logger = get_logger(__name__)


class PersonalBrainService:
    """Service for managing the Personal AI Brain."""

    def __init__(self, session: AsyncSession):
        """Initialize the service with dependencies."""
        self.session = session
        self.brain_repo = PersonalAIBrainRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.memory_repo = MemoryVaultRepository(session)
        self.knowledge_repo = KnowledgeGraphRepository(session)
        self.timeline_repo = TimelineRepository(session)

    async def get_brain_for_user(self, user_id: uuid.UUID) -> PersonalAIBrain | None:
        """Retrieve the personal brain for a user."""
        identity = await self.identity_repo.get_by_user_id(user_id)
        if not identity:
            return None
        return await self.brain_repo.get_by_ai_identity_id(identity.id)

    async def get_brain_by_ai_identity(self, ai_identity_id: uuid.UUID) -> PersonalAIBrain | None:
        """Retrieve the personal brain for an AI identity."""
        return await self.brain_repo.get_by_ai_identity_id(ai_identity_id)

    async def create_brain(
        self,
        ai_identity_id: uuid.UUID,
        brain_name: str,
        version: str = "1.0",
    ) -> PersonalAIBrain:
        """Create a new personal AI brain for an AI identity."""
        brain = await self.brain_repo.create(
            ai_identity_id=ai_identity_id,
            brain_name=brain_name,
            version=version,
            state="initializing",
        )
        await self.brain_repo.commit()
        logger.info(f"Created PersonalAIBrain {brain.id} for AIIdentity {ai_identity_id}")
        return brain

    async def get_brain_context(
        self, ai_identity_id: uuid.UUID
    ) -> dict:
        """Aggregate context for the personal brain."""
        brain = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        memory = await self.memory_repo.get_by_ai_identity_id(ai_identity_id)
        knowledge = await self.knowledge_repo.get_by_ai_identity_id(ai_identity_id)
        recent_timeline = await self.timeline_repo.get_recent_entries(
            ai_identity_id, hours=24, limit=50
        )

        return {
            "brain": {
                "id": str(brain.id) if brain else None,
                "state": brain.state if brain else None,
                "version": brain.version if brain else None,
            },
            "memory": {
                "object_reference": memory.object_reference if memory else None,
                "processing_status": memory.processing_status if memory else None,
            },
            "knowledge": {
                "graph_type": knowledge.graph_type if knowledge else None,
                "node_count": knowledge.node_count if knowledge else 0,
            },
            "recent_timeline_count": len(recent_timeline),
        }

    async def update_sync_version(
        self, ai_identity_id: uuid.UUID
    ) -> PersonalAIBrain | None:
        """Update the sync version of a brain."""
        brain = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        if not brain:
            return None
        
        from datetime import datetime, timezone
        updated = await self.brain_repo.update(
            brain.id,
            sync_version=brain.sync_version + 1,
            last_sync_at=datetime.now(timezone.utc),
        )
        await self.brain_repo.commit()
        return updated
