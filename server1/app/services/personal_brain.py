"""Personal AI Brain service layer for orchestration."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.brain_memory import BrainMemory, MemoryKind, MemorySource
from app.models.personal_ai_brain import PersonalAIBrain
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.brain_memory import BrainMemoryRepository
from app.repositories.knowledge_graph import KnowledgeGraphRepository
from app.repositories.memory_vault import MemoryVaultRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.repositories.timeline import TimelineRepository
from app.services.brain_provider import (
    GeneralLLMProvider,
    MockGeneralLLMProvider,
    MockPersonalBrainLLMProvider,
    MockPersonalVaultProvider,
    PersonalBrainLLMProvider,
    PersonalVaultProvider,
)
from app.services.real_brain_provider import RealPersonalBrainLLMProvider

logger = get_logger(__name__)


class PersonalBrainService:
    """Service for managing the Personal AI Brain and its provider boundary."""

    def __init__(
        self,
        session: AsyncSession,
        llm_provider: PersonalBrainLLMProvider | None = None,
        general_llm_provider: GeneralLLMProvider | None = None,
        vault_provider: PersonalVaultProvider | None = None,
    ):
        """Initialize the service with dependencies."""
        self.session = session
        self.brain_repo = PersonalAIBrainRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.memory_repo = MemoryVaultRepository(session)
        self.knowledge_repo = KnowledgeGraphRepository(session)
        self.timeline_repo = TimelineRepository(session)
        self.brain_memory_repo = BrainMemoryRepository(session)
        
        # Use real provider if INTERNAL_API_TOKEN is configured, otherwise use mock
        settings = get_settings()
        if llm_provider:
            self.llm_provider = llm_provider
        elif settings.internal_api_token:
            logger.info("Using RealPersonalBrainLLMProvider (Server 2 integration)")
            self.llm_provider = RealPersonalBrainLLMProvider()
        else:
            logger.info("Using MockPersonalBrainLLMProvider (no INTERNAL_API_TOKEN)")
            self.llm_provider = MockPersonalBrainLLMProvider()
        
        self.general_llm_provider = general_llm_provider or MockGeneralLLMProvider()
        self.vault_provider = vault_provider or MockPersonalVaultProvider()

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
        """Create the user's personal AI brain while enforcing one brain per identity."""
        existing = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        if existing is not None:
            return existing

        brain = await self.brain_repo.create(
            ai_identity_id=ai_identity_id,
            brain_name=brain_name,
            version=version,
            state="initializing",
        )
        await self.brain_repo.commit()
        logger.info(f"Created PersonalAIBrain {brain.id} for AIIdentity {ai_identity_id}")
        return brain

    async def prepare_external_context(self, user: User, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Package only the minimal context approved by the Personal Brain for a general LLM."""
        context = context or {}
        if hasattr(self.llm_provider, "prepare_external_context"):
            return await self.llm_provider.prepare_external_context(user, query, context)
        return await self.general_llm_provider.prepare_context(user, query, context)

    async def process_query(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
        *,
        use_memory: bool = False,
        persist_interaction: bool = False,
        memory_limit: int = 5,
    ) -> dict[str, Any]:
        """Route a user query through the Personal Brain provider while keeping vault access behind the brain boundary.

        Retrieval and persistence are explicit. Memories are only read when
        ``use_memory`` is set and only written when ``persist_interaction`` is
        set, so conversations are never saved automatically.
        """
        identity = None
        if use_memory or persist_interaction:
            identity = await self.identity_repo.get_by_user_id(user.id)

        memories: list[BrainMemory] = []
        if use_memory and identity is not None:
            memories = await self.brain_memory_repo.search_by_identity(identity.id, query, limit=memory_limit)
            if not memories:
                memories = await self.brain_memory_repo.get_recent_by_identity(identity.id, limit=memory_limit)

        enriched_context = dict(context or {})
        if memories:
            enriched_context["relevant_memories"] = [
                {"kind": mem.kind.value, "content": mem.content} for mem in memories
            ]
            # Only facts are forwarded as approved context; interpretations are
            # kept separate so the model does not treat them as ground truth.
            enriched_context["known_facts"] = [
                mem.content for mem in memories if mem.kind == MemoryKind.FACT
            ]

        prepared_context = await self.prepare_external_context(user, query, enriched_context)
        raw = await self.llm_provider.process_query(user, query, enriched_context)
        personalized = await self.llm_provider.personalize_response(
            user, raw.get("answer", query), enriched_context
        )

        persisted_memory_id = None
        if persist_interaction and identity is not None:
            memory = await self._create_memory(
                ai_identity_id=identity.id,
                content=personalized,
                kind=MemoryKind.INTERPRETATION,
                source=MemorySource.BRAIN,
                summary=f"Answer to: {query[:120]}",
                source_app="brain.query",
                importance=0,
            )
            persisted_memory_id = memory.id

        return {
            "answer": personalized,
            "source": raw.get("source", "mock_personal_brain_provider"),
            "context_used": bool(enriched_context),
            "user_id": str(user.id),
            "model": raw.get("model"),
            "latency_ms": raw.get("latency_ms"),
            "tokens_used": raw.get("tokens_used"),
            "minimal_external_context": prepared_context,
            "memories_used": [
                {"id": mem.id, "kind": mem.kind.value, "source": mem.source.value, "content": mem.content}
                for mem in memories
            ],
            "persisted_memory_id": persisted_memory_id,
        }

    async def _create_memory(
        self,
        ai_identity_id: uuid.UUID,
        content: str,
        kind: MemoryKind,
        source: MemorySource,
        summary: str | None = None,
        source_app: str | None = None,
        importance: int = 0,
    ) -> BrainMemory:
        """Create and commit a memory record."""
        memory = await self.brain_memory_repo.create(
            ai_identity_id=ai_identity_id,
            content=content,
            kind=kind,
            source=source,
            summary=summary,
            source_app=source_app,
            importance=importance,
        )
        await self.brain_memory_repo.commit()
        return memory

    async def store_memory_for_user(
        self,
        user: User,
        content: str,
        kind: MemoryKind = MemoryKind.FACT,
        summary: str | None = None,
        source_app: str | None = None,
        importance: int = 0,
    ) -> BrainMemory | None:
        """Explicitly persist a memory for a user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return None
        return await self._create_memory(
            ai_identity_id=identity.id,
            content=content,
            kind=kind,
            source=MemorySource.USER if kind == MemoryKind.FACT else MemorySource.BRAIN,
            summary=summary,
            source_app=source_app,
            importance=importance,
        )

    async def list_memories_for_user(
        self, user: User, limit: int = 100, offset: int = 0
    ) -> list[BrainMemory]:
        """List persisted memories for a user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return []
        return await self.brain_memory_repo.get_by_identity(identity.id, limit=limit, offset=offset)

    async def ingest_data(self, user: User, payload: dict[str, Any]) -> dict[str, Any]:
        """Classify and route application-ingested data through the Personal Brain provider.

        Ingestion is an explicit, application-initiated action, so it persists a
        ``fact`` memory owned by the user's AI identity.
        """
        classification = await self.llm_provider.classify_information(user, payload)
        vault_result = await self.vault_provider.store_ingestion(user, payload)

        memory = await self.store_memory_for_user(
            user,
            content=str(payload.get("content", "")),
            kind=MemoryKind.FACT,
            summary=classification.get("summary"),
            source_app=payload.get("source_app"),
            importance=10,
        )

        return {
            "accepted": True,
            "classification": classification,
            "stored": vault_result.get("stored", True),
            "vault_reference": vault_result,
            "memory_id": memory.id if memory else None,
        }

    async def get_brain_context(self, ai_identity_id: uuid.UUID) -> dict:
        """Aggregate context for the personal brain."""
        brain = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        memory = await self.memory_repo.get_by_ai_identity_id(ai_identity_id)
        knowledge = await self.knowledge_repo.get_by_ai_identity_id(ai_identity_id)
        recent_timeline = await self.timeline_repo.get_recent_entries(ai_identity_id, hours=24, limit=50)

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


    async def update_memory_for_user(
        self,
        user: User,
        memory_id: uuid.UUID,
        content: str | None = None,
        summary: str | None = None,
        importance: int | None = None,
    ) -> BrainMemory | None:
        """Update a memory owned by the user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return None
        memory = await self.brain_memory_repo.get_by_id(memory_id)
        if memory is None or memory.ai_identity_id != identity.id:
            return None
        updates = {}
        if content is not None:
            updates["content"] = content
        if summary is not None:
            updates["summary"] = summary
        if importance is not None:
            updates["importance"] = importance
        if not updates:
            return memory
        updated = await self.brain_memory_repo.update(memory_id, **updates)
        await self.brain_memory_repo.commit()
        return updated

    async def delete_memory_for_user(self, user: User, memory_id: uuid.UUID) -> bool:
        """Delete a memory owned by the user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return False
        memory = await self.brain_memory_repo.get_by_id(memory_id)
        if memory is None or memory.ai_identity_id != identity.id:
            return False
        result = await self.brain_memory_repo.delete(memory_id)
        await self.brain_memory_repo.commit()
        return result

    async def get_memory_for_user(self, user: User, memory_id: uuid.UUID) -> BrainMemory | None:
        """Get a memory owned by the user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return None
        memory = await self.brain_memory_repo.get_by_id(memory_id)
        if memory is None or memory.ai_identity_id != identity.id:
            return None
        return memory

    async def search_memories_for_user(
        self, user: User, query: str, limit: int = 10
    ) -> list[BrainMemory]:
        """Search memories for a user's AI identity."""
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return []
        return await self.brain_memory_repo.search_by_identity(identity.id, query, limit=limit)

    async def update_sync_version(self, ai_identity_id: uuid.UUID) -> PersonalAIBrain | None:
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
