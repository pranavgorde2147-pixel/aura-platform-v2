"""The personal vault of one Brain.

Every read and write is filtered on ``brain_id`` **and** ``owner_id**. A
record id alone is never sufficient: asking for another Brain's record with a
valid id returns "not found" rather than the record.

The vault does not duplicate the canonical application records that Server 1
owns. It stores the Brain-local record entries and pointers into the object
store, plus the Brain's own vector index and relationship graph.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import (
    BrainVaultEmbedding,
    BrainVaultRecord,
    BrainVaultRelationship,
    BrainVectorSpace,
)
from app.brain_manager.registry.persistence import get_or_create
from app.brain_manager.vault.embeddings import (
    MODEL_ID,
    HashingEmbeddingProvider,
    cosine_similarity,
    get_embedding_provider,
)
from app.storage_db import ManagedBrain


class VaultRecordNotFound(LookupError):
    """Raised when a record does not exist inside the requested Brain."""


class CrossBrainAccessError(PermissionError):
    """Raised when a caller tries to reach a resource of another Brain."""


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def chunk_text(text: str, *, size: int = 800, overlap: int = 80) -> list[str]:
    """Split text into overlapping chunks for embedding."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(cleaned):
        chunks.append(cleaned[start : start + size])
        start += step
    return chunks


class BrainVaultService:
    """Owner- and Brain-scoped access to one Brain's vault."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedding_provider: HashingEmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.embedding_provider = embedding_provider or get_embedding_provider()

    # -- initialisation ----------------------------------------------------
    async def ensure_initialized(self, brain: ManagedBrain) -> BrainVectorSpace:
        """Create the Brain's vector-space descriptor if it does not exist.

        The insert is guarded by the unique constraint on ``brain_id`` so that
        two concurrent provisioning requests for the same Brain converge on a
        single vector space instead of failing on a duplicate row.
        """
        from sqlalchemy import select

        async def _fetch() -> BrainVectorSpace | None:
            result = await self.session.execute(
                select(BrainVectorSpace).where(
                    BrainVectorSpace.brain_id == brain.brain_id
                )
            )
            return result.scalars().first()

        space = await get_or_create(
            self.session,
            model=BrainVectorSpace,
            lookup={"brain_id": brain.brain_id},
            create=lambda: BrainVectorSpace(
                brain_id=brain.brain_id,
                owner_id=brain.owner_id,
                dimension=self.embedding_provider.dimension,
                model_id=MODEL_ID,
                status="ready",
                retrieval_config={
                    "top_k": 10,
                    "min_score": 0.05,
                    "max_context_records": 10,
                },
            ),
            fetch=_fetch,
        )
        await self.session.commit()
        await self.session.refresh(space)
        return space

    # -- records -----------------------------------------------------------
    async def put_record(
        self,
        brain: ManagedBrain,
        *,
        record_type: str,
        title: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        category: str | None = None,
        source_app: str | None = None,
        source_reference: str | None = None,
        object_key: str | None = None,
        access_policy: str = "owner_only",
    ) -> BrainVaultRecord:
        """Store one record in the Brain's vault and index it."""
        record = BrainVaultRecord(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            record_type=record_type,
            category=category,
            title=title,
            content=content,
            object_key=object_key,
            source_app=source_app,
            source_reference=source_reference,
            access_policy=access_policy,
            record_metadata=metadata or {},
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        await self.index_record(brain, record)
        return record

    async def get_record(
        self, brain: ManagedBrain, record_id: uuid.UUID
    ) -> BrainVaultRecord:
        """Fetch one record, enforcing Brain and owner ownership."""
        result = await self.session.execute(
            select(BrainVaultRecord).where(
                BrainVaultRecord.id == record_id,
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
            )
        )
        record = result.scalars().first()
        if record is None:
            raise VaultRecordNotFound(
                "The requested record does not exist in this Brain"
            )
        return record

    async def list_records(
        self,
        brain: ManagedBrain,
        *,
        record_type: str | None = None,
        source_app: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BrainVaultRecord], int]:
        """List the records of one Brain."""
        filters = [
            BrainVaultRecord.brain_id == brain.brain_id,
            BrainVaultRecord.owner_id == brain.owner_id,
        ]
        if record_type:
            filters.append(BrainVaultRecord.record_type == record_type)
        if source_app:
            filters.append(BrainVaultRecord.source_app == source_app)

        base = select(BrainVaultRecord).where(*filters)
        total = int(
            await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        )
        result = await self.session.execute(
            base.order_by(BrainVaultRecord.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), total

    async def delete_record(self, brain: ManagedBrain, record_id: uuid.UUID) -> bool:
        """Delete one record and its embeddings."""
        try:
            record = await self.get_record(brain, record_id)
        except VaultRecordNotFound:
            return False
        await self.session.execute(
            delete(BrainVaultEmbedding).where(BrainVaultEmbedding.record_id == record.id)
        )
        await self.session.execute(
            delete(BrainVaultRelationship).where(
                BrainVaultRelationship.source_record_id == record.id
            )
        )
        await self.session.delete(record)
        await self.session.commit()
        return True

    # -- relationships -----------------------------------------------------
    async def link_records(
        self,
        brain: ManagedBrain,
        *,
        source_record_id: uuid.UUID,
        target_record_id: uuid.UUID | None = None,
        target_app: str | None = None,
        relation_type: str = "related_to",
    ) -> BrainVaultRelationship:
        """Create a relationship between two records of the same Brain."""
        if bool(target_record_id) == bool(target_app):
            raise ValueError("Exactly one of target_record_id or target_app must be given")
        source = await self.get_record(brain, source_record_id)
        if target_record_id is not None:
            target = await self.get_record(brain, target_record_id)
            if target.id == source.id:
                raise ValueError("A record cannot be related to itself")
        relationship = BrainVaultRelationship(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            source_record_id=source.id,
            target_record_id=target_record_id,
            target_app=target_app,
            relation_type=relation_type,
        )
        self.session.add(relationship)
        await self.session.commit()
        await self.session.refresh(relationship)
        return relationship

    async def list_relationships(
        self, brain: ManagedBrain, *, record_id: uuid.UUID | None = None
    ) -> list[BrainVaultRelationship]:
        query = select(BrainVaultRelationship).where(
            BrainVaultRelationship.brain_id == brain.brain_id,
            BrainVaultRelationship.owner_id == brain.owner_id,
        )
        if record_id is not None:
            query = query.where(BrainVaultRelationship.source_record_id == record_id)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    # -- vector index ------------------------------------------------------
    async def index_record(
        self, brain: ManagedBrain, record: BrainVaultRecord
    ) -> int:
        """Embed a record into the Brain's own vector index."""
        await self.ensure_initialized(brain)
        text = record.content or record.title or ""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        await self.session.execute(
            delete(BrainVaultEmbedding).where(
                BrainVaultEmbedding.brain_id == brain.brain_id,
                BrainVaultEmbedding.record_id == record.id,
            )
        )
        vectors = self.embedding_provider.embed(chunks)
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            self.session.add(
                BrainVaultEmbedding(
                    brain_id=brain.brain_id,
                    owner_id=brain.owner_id,
                    record_id=record.id,
                    chunk_index=index,
                    dimension=len(vector),
                    model_id=MODEL_ID,
                    vector=[float(value) for value in vector],
                    content_hash=_content_hash(chunk),
                )
            )
        await self.session.commit()
        return len(chunks)

    async def semantic_search(
        self,
        brain: ManagedBrain,
        query: str,
        *,
        limit: int = 10,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search the Brain's own vector index.

        The query is always filtered on ``brain_id`` and ``owner_id``, so a
        result can never belong to another Brain even if a caller supplies a
        record id from elsewhere.
        """
        if not query or not query.strip():
            return []
        space = await self.ensure_initialized(brain)
        threshold = (
            min_score
            if min_score is not None
            else float((space.retrieval_config or {}).get("min_score", 0.05))
        )
        query_vector = self.embedding_provider.embed([query])[0]
        result = await self.session.execute(
            select(BrainVaultEmbedding, BrainVaultRecord)
            .join(
                BrainVaultRecord,
                BrainVaultRecord.id == BrainVaultEmbedding.record_id,
            )
            .where(
                BrainVaultEmbedding.brain_id == brain.brain_id,
                BrainVaultEmbedding.owner_id == brain.owner_id,
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
            )
        )
        scored: list[dict[str, Any]] = []
        for embedding, record in result.all():
            score = cosine_similarity(query_vector, list(embedding.vector or []))
            if score < threshold:
                continue
            scored.append(
                {
                    "record_id": str(record.id),
                    "record_type": record.record_type,
                    "title": record.title,
                    "source_app": record.source_app,
                    "score": round(float(score), 6),
                    "created_at": record.created_at.isoformat()
                    if record.created_at
                    else None,
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(1, limit)]

    async def vector_status(self, brain: ManagedBrain) -> dict[str, Any]:
        """Return the status of the Brain's vector index."""
        space_result = await self.session.execute(
            select(BrainVectorSpace).where(BrainVectorSpace.brain_id == brain.brain_id)
        )
        space = space_result.scalars().first()
        embedding_count = int(
            await self.session.scalar(
                select(func.count(BrainVaultEmbedding.id)).where(
                    BrainVaultEmbedding.brain_id == brain.brain_id,
                    BrainVaultEmbedding.owner_id == brain.owner_id,
                )
            )
            or 0
        )
        record_count = int(
            await self.session.scalar(
                select(func.count(BrainVaultRecord.id)).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                )
            )
            or 0
        )
        return {
            "status": space.status if space is not None else "not_initialized",
            "dimension": space.dimension if space is not None else self.embedding_provider.dimension,
            "model_id": space.model_id if space is not None else MODEL_ID,
            "embedding_count": embedding_count,
            "record_count": record_count,
            "retrieval_config": (space.retrieval_config if space is not None else {}) or {},
        }

    async def count_records(self, brain: ManagedBrain) -> int:
        """Return the number of records stored in the Brain's vault."""
        return int(
            await self.session.scalar(
                select(func.count(BrainVaultRecord.id)).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                )
            )
            or 0
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BrainVaultService",
    "CrossBrainAccessError",
    "HashingEmbeddingProvider",
    "VaultRecordNotFound",
    "chunk_text",
    "cosine_similarity",
    "utcnow",
]
