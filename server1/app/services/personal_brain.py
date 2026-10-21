"""Personal AI Brain service layer for orchestration."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.storage_client import StorageServiceError, get_storage_client
from app.models.brain_memory import BrainMemory, MemoryKind, MemorySource
from app.models.personal_ai_brain import PersonalAIBrain
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.app_record import AppRecordRepository
from app.repositories.brain_memory import BrainMemoryRepository
from app.repositories.knowledge_graph import KnowledgeGraphRepository
from app.repositories.memory_vault import MemoryVaultRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.repositories.timeline import TimelineRepository
from app.services.app_ingestion import AppAdapter, NormalizedIngestion
from app.services.brain_provider import (
    GeneralLLMProvider,
    MockGeneralLLMProvider,
    MockPersonalBrainLLMProvider,
    MockPersonalVaultProvider,
    PersonalBrainLLMProvider,
    PersonalVaultProvider,
)
from app.services.memory_index import MemoryIndexer, NoOpMemoryIndexer, get_memory_indexer
from app.services.real_brain_provider import RealPersonalBrainLLMProvider
from app.services.storage_adapters import (
    RecordPayload,
    build_dedupe_key,
    build_record_store,
)

logger = get_logger(__name__)

#: Maximum number of characters of an ingested item that Brain memory keeps.
#: The complete content lives in the application record store.
MEMORY_CONTENT_LIMIT = 1000
QUERY_CONTEXT_RECORD_LIMIT = 4000


class AppIngestionPersistenceError(Exception):
    """The canonical application record could not be durably persisted."""


class BrainRecordRetrievalError(Exception):
    """A selected canonical record could not be retrieved for Brain context."""


def _bounded_memory_content(normalized: NormalizedIngestion) -> str:
    """Return the bounded text that may be stored in Brain memory."""
    text = normalized.summary or normalized.content or ""
    if len(text) <= MEMORY_CONTENT_LIMIT:
        return text
    return text[:MEMORY_CONTENT_LIMIT].rstrip()


class PersonalBrainService:
    """Service for managing the Personal AI Brain and its provider boundary."""

    def __init__(
        self,
        session: AsyncSession,
        llm_provider: PersonalBrainLLMProvider | None = None,
        general_llm_provider: GeneralLLMProvider | None = None,
        vault_provider: PersonalVaultProvider | None = None,
        memory_indexer: MemoryIndexer | NoOpMemoryIndexer | None = None,
    ):
        """Initialize the service with dependencies."""
        self.session = session
        self.brain_repo = PersonalAIBrainRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.memory_repo = MemoryVaultRepository(session)
        self.knowledge_repo = KnowledgeGraphRepository(session)
        self.timeline_repo = TimelineRepository(session)
        self.brain_memory_repo = BrainMemoryRepository(session)
        self.app_record_repo = AppRecordRepository(session)

        # Semantic retrieval is optional; a no-op indexer keeps keyword search
        # working when Qdrant or embeddings are unavailable.
        if memory_indexer is not None:
            self.memory_indexer = memory_indexer
        elif llm_provider is not None:
            # Callers injecting a provider (tests, custom setups) get an inert
            # indexer unless they explicitly opt in.
            self.memory_indexer = NoOpMemoryIndexer()
        else:
            self.memory_indexer = get_memory_indexer()
        
        # The real provider is used whenever INTERNAL_API_TOKEN is configured.
        # Without a token (local development, tests) a clearly-labelled, offline
        # mock keeps the service running; it must never be mistaken for real
        # inference, hence the loud log and the explicit non-production source.
        settings = get_settings()
        if llm_provider:
            self.llm_provider = llm_provider
        elif settings.internal_api_token:
            logger.info("Using RealPersonalBrainLLMProvider (Server 2 integration)")
            self.llm_provider = RealPersonalBrainLLMProvider()
        else:
            if settings.environment.lower() == "production":
                logger.error(
                    "INTERNAL_API_TOKEN is not configured while ENVIRONMENT=production; "
                    "falling back to the development mock brain provider"
                )
            else:
                logger.warning("Using MockPersonalBrainLLMProvider (no INTERNAL_API_TOKEN)")
            self.llm_provider = MockPersonalBrainLLMProvider()

        # General-LLM and vault boundaries are optional integrations. They only
        # default to the mock implementation when no real provider is injected,
        # and their output is never treated as authoritative (see ingest_data).
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
        use_memory: bool = True,
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
        records = []
        if use_memory and identity is not None:
            memories = await self._retrieve_memories(identity.id, query, memory_limit)
            records = await self.app_record_repo.search_for_user(
                user_id=user.id,
                query=query,
                limit=memory_limit,
            )

        record_contents: dict[uuid.UUID, str] = {}
        for record in records:
            if record.content is not None:
                record_contents[record.id] = record.content
            elif record.storage_type == "object" and record.object_reference:
                try:
                    record_contents[record.id] = await get_storage_client().read_text_object(
                        owner_id=str(user.id),
                        object_id=record.object_reference,
                        max_bytes=200_000,
                    )
                except StorageServiceError as exc:
                    raise BrainRecordRetrievalError(
                        "A selected canonical record could not be retrieved."
                    ) from exc
            else:
                raise BrainRecordRetrievalError(
                    "A selected canonical record has no retrievable content."
                )

        reserved_context_keys = {
            "authorized_memories",
            "authorized_records",
            "known_facts",
            "relevant_memories",
        }
        enriched_context = {
            key: value
            for key, value in (context or {}).items()
            if key not in reserved_context_keys
        }
        if memories:
            enriched_context["relevant_memories"] = [
                {
                    "kind": mem.kind.value,
                    "content": mem.content,
                    "source_app": mem.source_app,
                }
                for mem in memories
            ]
            enriched_context["authorized_memories"] = [
                {
                    "id": str(mem.id),
                    "kind": mem.kind.value,
                    "source": mem.source.value,
                    "source_app": mem.source_app,
                    "content": mem.content,
                }
                for mem in memories
            ]
            # Only facts are forwarded as approved context; interpretations are
            # kept separate so the model does not treat them as ground truth.
            enriched_context["known_facts"] = [
                mem.content for mem in memories if mem.kind == MemoryKind.FACT
            ]
        if records:
            enriched_context["authorized_records"] = [
                {
                    "record_id": str(record.id),
                    "source_app": record.destination_app,
                    "record_type": record.record_type,
                    "source_reference": record.source_reference,
                    "content": _record_context_excerpt(record_contents[record.id], query),
                }
                for record in records
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
            "source": raw.get("source", "unknown"),
            "context_used": bool(enriched_context),
            "user_id": str(user.id),
            "model": raw.get("model"),
            "latency_ms": raw.get("latency_ms"),
            "tokens_used": raw.get("tokens_used"),
            "error_code": raw.get("error_code"),
            "minimal_external_context": prepared_context,
            "memories_used": [
                {
                    "id": mem.id,
                    "kind": mem.kind.value,
                    "source": mem.source.value,
                    "content": mem.content,
                    "source_app": mem.source_app,
                }
                for mem in memories
            ],
            "records_used": [
                {
                    "id": record.id,
                    "source_app": record.destination_app,
                    "record_type": record.record_type,
                    "source_reference": record.source_reference,
                    "content": _record_context_excerpt(record_contents[record.id], query),
                }
                for record in records
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
        """Create a memory record and keep the vector index in sync."""
        memory = await self.brain_memory_repo.create(
            ai_identity_id=ai_identity_id,
            content=content,
            kind=kind,
            source=source,
            summary=summary,
            source_app=source_app,
            importance=importance,
        )
        if self.memory_indexer.enabled:
            indexed = await self.memory_indexer.index_memory(
                memory_id=memory.id,
                ai_identity_id=ai_identity_id,
                content=content,
                payload={
                    "kind": kind.value,
                    "source": source.value,
                    "source_app": source_app,
                    "importance": importance,
                },
            )
            memory.embedding_status = "embedded" if indexed else "failed"
        await self.brain_memory_repo.commit()
        return memory

    async def _retrieve_memories(
        self, ai_identity_id: uuid.UUID, query: str, limit: int
    ) -> list[BrainMemory]:
        """Retrieve memories semantically when possible, else by keyword.

        Vector hits are re-fetched from Postgres (scoped to the identity) so the
        authoritative record and its provenance are always returned.
        """
        if self.memory_indexer.enabled:
            hits = await self.memory_indexer.search(ai_identity_id, query, limit)
            if hits:
                ordered_ids = [memory_id for memory_id, _ in hits]
                stored = await self.brain_memory_repo.get_by_ids(ai_identity_id, ordered_ids)
                by_id = {memory.id: memory for memory in stored}
                ranked = [by_id[memory_id] for memory_id in ordered_ids if memory_id in by_id]
                if ranked:
                    return ranked
        return await self.brain_memory_repo.search_by_identity(ai_identity_id, query, limit=limit)

    async def store_memory_for_user(
        self,
        user: User,
        content: str,
        kind: MemoryKind = MemoryKind.FACT,
        summary: str | None = None,
        source_app: str | None = None,
        importance: int = 0,
        source: MemorySource | None = None,
    ) -> BrainMemory | None:
        """Explicitly persist a memory for a user's AI identity.

        ``source`` defaults to USER for facts and BRAIN for interpretations;
        application ingestion passes ``MemorySource.APP`` explicitly.
        """
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return None
        if source is None:
            source = MemorySource.USER if kind == MemoryKind.FACT else MemorySource.BRAIN
        return await self._create_memory(
            ai_identity_id=identity.id,
            content=content,
            kind=kind,
            source=source,
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

    async def _record_knowledge(
        self, ai_identity_id: uuid.UUID, classification: dict[str, Any]
    ) -> Any:
        """Increment the identity's knowledge graph node count for this ingest."""
        labels = classification.get("labels") or []
        nodes = max(1, len(labels))
        metadata = json.dumps(
            {"last_content_type": classification.get("content_type")},
            ensure_ascii=False,
        )
        return await self.knowledge_repo.record_nodes(
            ai_identity_id, nodes=nodes, graph_metadata=metadata
        )

    async def _record_timeline(
        self,
        ai_identity_id: uuid.UUID,
        *,
        event_type: str,
        resource_type: str | None,
        resource_reference: str | None,
        summary: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Append a dated timeline event for the identity."""
        return await self.timeline_repo.create(
            ai_identity_id=ai_identity_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_reference=resource_reference,
            summary=summary,
            event_metadata=json.dumps(metadata, ensure_ascii=False) if metadata else None,
        )

    async def record_ingestion(
        self,
        user: User,
        *,
        normalized: NormalizedIngestion,
        source_app: str | None,
    ) -> dict[str, Any]:
        """Persist a normalized ingestion and its brain side effects.

        The write is idempotent when ``normalized.dedupe_key`` is set: a prior
        timeline event with the same ``(event_type, dedupe_key)`` short-circuits
        the whole operation. Canonical app content is written before Brain
        memory; failure to store it aborts ingestion. Vault / knowledge /
        timeline side effects follow and, if any fail, the primary record and
        memory are preserved and ``partial`` is reported rather than raising.
        """
        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            return {
                "accepted": False,
                "reason": "brain_not_found",
                "event_type": normalized.event_type,
                "memory_id": None,
                "vault_entry_id": None,
            }

        if normalized.dedupe_key:
            existing = await self.timeline_repo.find_by_reference(
                identity.id, normalized.event_type, normalized.dedupe_key
            )
            if existing is not None:
                return {
                    "accepted": True,
                    "duplicate": True,
                    "event_type": normalized.event_type,
                    "memory_id": _metadata_memory_id(existing.event_metadata),
                    "vault_entry_id": None,
                }

        memory = None
        record_id: str | None = None
        partial = False
        if normalized.persist_memory:
            # The full content belongs to the *application* storage, not to
            # Brain memory. Brain keeps only a bounded summary so it can be
            # used for future reasoning without duplicating the user's
            # original content.
            try:
                record_id = await self._store_application_record(
                    user,
                    normalized=normalized,
                    source_app=source_app,
                )
            except Exception as exc:  # noqa: BLE001 - translate persistence failures safely
                logger.warning(
                    "Canonical record persistence failed for app %s: %s",
                    source_app,
                    type(exc).__name__,
                )
                raise AppIngestionPersistenceError(
                    "The application record could not be durably stored"
                ) from exc
            memory = await self.store_memory_for_user(
                user,
                content=_bounded_memory_content(normalized),
                kind=normalized.kind,
                summary=normalized.summary,
                source_app=source_app,
                importance=normalized.importance,
                source=MemorySource.APP,
            )

        vault_entry = None
        try:
            if normalized.vault and normalized.vault.get("object_reference"):
                vault_entry = await self.memory_repo.upsert_for_identity(
                    identity.id, **_vault_fields(normalized.vault)
                )
            await self._record_knowledge(
                identity.id,
                {
                    "labels": list(normalized.labels),
                    "content_type": normalized.event_type,
                },
            )
            await self._record_timeline(
                identity.id,
                event_type=normalized.event_type,
                resource_type=normalized.resource_type,
                resource_reference=normalized.dedupe_key
                or (str(memory.id) if memory else None),
                summary=normalized.summary,
                metadata={
                    "source_app": source_app,
                    "memory_id": str(memory.id) if memory else None,
                    "dedupe_key": normalized.dedupe_key,
                    **(normalized.metadata or {}),
                },
            )
            await self.timeline_repo.commit()
        except Exception as exc:  # noqa: BLE001 - side effects must not lose the memory
            partial = True
            logger.warning(
                "Partial ingestion for app %s (memory preserved): %s",
                source_app,
                type(exc).__name__,
            )
            try:
                await self.timeline_repo.rollback()
            except Exception:  # pragma: no cover - rollback is best-effort
                pass

        return {
            "accepted": True,
            "duplicate": False,
            "partial": partial,
            "event_type": normalized.event_type,
            "memory_id": str(memory.id) if memory else None,
            "vault_entry_id": str(vault_entry.id) if vault_entry else None,
            "record_id": record_id,
        }

    async def _store_application_record(
        self,
        user: User,
        *,
        normalized: NormalizedIngestion,
        source_app: str | None,
    ) -> str | None:
        """Store the full ingested content in the application record store.

        Application storage keeps the complete content of an ingested item
        (lecture notes, meeting transcripts, diary entries). Brain memory only
        ever receives a bounded summary of it.
        """
        destination = source_app or "brain"
        record_type = normalized.resource_type or "record"
        content = normalized.content or ""
        dedupe_key = build_dedupe_key(
            user_id=user.id,
            destination_app=destination,
            record_type=record_type,
            run_id=None,
            segment_index=None,
            content=content,
        )
        if normalized.dedupe_key:
            dedupe_key = f"{destination}:{record_type}:{normalized.dedupe_key}"
        store = build_record_store(self.session, "relational", self.app_record_repo)
        stored = await store.write(
            RecordPayload(
                user_id=user.id,
                destination_app=destination,
                record_type=record_type,
                content=content,
                dedupe_key=dedupe_key,
                title=normalized.summary,
                summary=normalized.summary,
                source_reference=(
                    (normalized.vault or {}).get("object_reference")
                    if normalized.vault
                    else normalized.dedupe_key
                ),
                retention_policy="default",
            )
        )
        return str(stored.record_id)

    async def ingest_app_event(
        self,
        user: User,
        adapter: AppAdapter,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize and persist one application event through the brain boundary."""
        normalized = adapter.normalize(payload)
        return await self.record_ingestion(
            user, normalized=normalized, source_app=adapter.slug
        )

    async def ingest_data(self, user: User, payload: dict[str, Any]) -> dict[str, Any]:
        """Classify and route application-ingested data through the Personal Brain.

        Generic ingestion used by ``POST /api/v1/brain/ingest``. Adapters with
        stricter, app-specific shapes use :meth:`ingest_app_event` instead.
        """
        classification = await self.llm_provider.classify_information(user, payload)
        # Informational boundary call only. The authoritative vault write happens
        # in ``record_ingestion`` via ``MemoryVaultRepository``; the provider's
        # reply (which may be a development mock) is deliberately not used to
        # report success.
        await self.vault_provider.store_ingestion(user, payload)

        flat_vault = {**payload, **(payload.get("metadata") or {})}
        normalized = NormalizedIngestion(
            content=str(payload.get("content", "")),
            kind=MemoryKind.FACT,
            event_type="memory.ingested",
            summary=classification.get("summary"),
            resource_type="memory",
            labels=tuple(classification.get("labels") or []),
            importance=10,
            vault=flat_vault,
            metadata={"content_type": classification.get("content_type")},
        )
        result = await self.record_ingestion(
            user, normalized=normalized, source_app=payload.get("source_app")
        )

        return {
            "accepted": result["accepted"],
            "classification": classification,
            # ``stored`` reflects the real write (memory and/or vault pointer),
            # never the mock provider's optimistic reply.
            "stored": result["accepted"],
            "memory_id": result.get("memory_id"),
            "vault_entry_id": result.get("vault_entry_id"),
            "record_id": result.get("record_id"),
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
        if updated is not None and "content" in updates and self.memory_indexer.enabled:
            await self.memory_indexer.index_memory(
                memory_id=updated.id,
                ai_identity_id=identity.id,
                content=updated.content,
                payload={
                    "kind": updated.kind.value,
                    "source": updated.source.value,
                    "source_app": updated.source_app,
                    "importance": updated.importance,
                },
            )
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
        if result and self.memory_indexer.enabled:
            await self.memory_indexer.delete(memory_id)
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
        return await self._retrieve_memories(identity.id, query, limit)

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


def _vault_fields(vault: dict[str, Any]) -> dict[str, Any]:
    """Map a normalized vault pointer dict to repository keyword arguments."""
    size = vault.get("file_size_bytes")
    return {
        "object_reference": str(vault["object_reference"]),
        "storage_type": str(vault.get("storage_type") or "object"),
        "checksum": vault.get("checksum"),
        "mime_type": vault.get("mime_type"),
        "file_size_bytes": int(size) if size is not None else None,
        "processing_status": str(vault.get("processing_status") or "stored"),
    }


def _metadata_memory_id(event_metadata: str | None) -> str | None:
    """Extract ``memory_id`` from a stored timeline event's JSON metadata."""
    if not event_metadata:
        return None
    try:
        data = json.loads(event_metadata)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        value = data.get("memory_id")
        return str(value) if value else None
    return None


def _record_context_excerpt(content: str, query: str) -> str:
    """Bound source-record text while centering an excerpt on query terms."""
    if len(content) <= QUERY_CONTEXT_RECORD_LIMIT:
        return content
    terms = [
        term
        for term in query.casefold().split()
        if len(term) > 1 and term not in {"what", "when", "where", "which", "who", "why", "how", "the", "is", "my"}
    ]
    content_lower = content.casefold()
    match_positions = [content_lower.find(term) for term in terms]
    match_positions = [position for position in match_positions if position >= 0]
    center = min(match_positions) if match_positions else 0
    start = max(0, center - QUERY_CONTEXT_RECORD_LIMIT // 4)
    end = min(len(content), start + QUERY_CONTEXT_RECORD_LIMIT)
    excerpt = content[start:end]
    if start:
        excerpt = "..." + excerpt
    if end < len(content):
        excerpt += "..."
    return excerpt
