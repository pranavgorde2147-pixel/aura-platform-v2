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
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import (
    BrainVaultEmbedding,
    BrainVaultRecord,
    BrainVaultRecordVersion,
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
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.storage_db import ManagedBrain


def _private_vault_operation(method):
    """Run a public vault operation in its Brain's dedicated SQLite session."""
    @wraps(method)
    async def wrapped(self, brain: ManagedBrain, *args, **kwargs):
        if self._active_session.get() is not None:
            return await method(self, brain, *args, **kwargs)
        async with get_brain_vault_storage().session(brain) as session:
            token = self._active_session.set(session)
            try:
                return await method(self, brain, *args, **kwargs)
            finally:
                self._active_session.reset(token)

    return wrapped


class VaultRecordNotFound(LookupError):
    """Raised when a record does not exist inside the requested Brain."""


class CrossBrainAccessError(PermissionError):
    """Raised when a caller tries to reach a resource of another Brain."""


class IdempotencyConflict(ValueError):
    """Raised when an idempotency key is reused for different content."""


class RecordVersionConflict(ValueError):
    """Raised when a caller attempts to update a stale record version."""


def validate_namespace(namespace: str, source_app: str | None) -> str:
    """Allow only shared/document namespaces or the supplied app's namespace."""
    if namespace in {"shared", "documents", "conversations"}:
        return namespace
    match = re.fullmatch(r"apps/([a-z0-9][a-z0-9-]{0,99})", namespace)
    if match is None or source_app != match.group(1):
        raise ValueError("namespace must match the authorized source app")
    return namespace


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
        session: AsyncSession | None = None,
        *,
        embedding_provider: HashingEmbeddingProvider | None = None,
    ) -> None:
        self._registry_session = session
        self._active_session: ContextVar[AsyncSession | None] = ContextVar(
            f"brain_vault_session_{id(self)}", default=None
        )
        self.embedding_provider = embedding_provider or get_embedding_provider()

    @property
    def session(self) -> AsyncSession:
        session = self._active_session.get() or self._registry_session
        if session is None:
            raise RuntimeError("Vault operation is not bound to a private session")
        return session

    # -- initialisation ----------------------------------------------------
    @_private_vault_operation
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
    @_private_vault_operation
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
        namespace: str = "shared",
        idempotency_key: str | None = None,
    ) -> BrainVaultRecord:
        """Store one record in the Brain's vault and index it."""
        namespace = validate_namespace(namespace, source_app)
        if idempotency_key:
            result = await self.session.execute(
                select(BrainVaultRecord).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                    BrainVaultRecord.idempotency_key == idempotency_key,
                )
            )
            existing = result.scalars().first()
            if existing is not None:
                if existing.deleted_at is not None:
                    raise IdempotencyConflict(
                        "idempotency key belongs to a deleted record"
                    )
                if (
                    existing.content != content
                    or existing.title != title
                    or existing.namespace != namespace
                    or existing.record_type != record_type
                ):
                    raise IdempotencyConflict(
                        "idempotency key already identifies different record data"
                    )
                await self.index_record(brain, existing)
                return existing
        record = BrainVaultRecord(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            namespace=namespace,
            idempotency_key=idempotency_key,
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
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            if idempotency_key is None:
                raise
            existing_result = await self.session.execute(
                select(BrainVaultRecord).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                    BrainVaultRecord.idempotency_key == idempotency_key,
                )
            )
            existing = existing_result.scalars().first()
            if existing is None:
                raise
            if (
                existing.deleted_at is not None
                or existing.content != content
                or existing.title != title
                or existing.namespace != namespace
                or existing.record_type != record_type
            ):
                raise IdempotencyConflict(
                    "idempotency key already identifies different record data"
                ) from exc
            await self.index_record(brain, existing)
            return existing
        await self.session.refresh(record)
        await self.index_record(brain, record)
        return record

    @_private_vault_operation
    async def get_record(
        self, brain: ManagedBrain, record_id: uuid.UUID
    ) -> BrainVaultRecord:
        """Fetch one record, enforcing Brain and owner ownership."""
        result = await self.session.execute(
            select(BrainVaultRecord).where(
                BrainVaultRecord.id == record_id,
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.deleted_at.is_(None),
            )
        )
        record = result.scalars().first()
        if record is None:
            raise VaultRecordNotFound(
                "The requested record does not exist in this Brain"
            )
        return record

    @_private_vault_operation
    async def list_records(
        self,
        brain: ManagedBrain,
        *,
        record_type: str | None = None,
        source_app: str | None = None,
        namespace: str | None = None,
        source_reference: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BrainVaultRecord], int]:
        """List the records of one Brain."""
        filters = [
            BrainVaultRecord.brain_id == brain.brain_id,
            BrainVaultRecord.owner_id == brain.owner_id,
            BrainVaultRecord.deleted_at.is_(None),
        ]
        if record_type:
            filters.append(BrainVaultRecord.record_type == record_type)
        if source_app:
            filters.append(BrainVaultRecord.source_app == source_app)
        if namespace:
            filters.append(BrainVaultRecord.namespace == namespace)
        if source_reference:
            filters.append(BrainVaultRecord.source_reference == source_reference)

        base = select(BrainVaultRecord).where(*filters)
        total = int(
            await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        )
        result = await self.session.execute(
            base.order_by(BrainVaultRecord.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), total

    @_private_vault_operation
    async def text_replacement_matches(
        self,
        brain: ManagedBrain,
        *,
        namespaces: list[str],
        find_text: str,
    ) -> list[dict[str, Any]]:
        """Return exact, case-sensitive title/content matches in app spaces."""
        if not find_text:
            raise ValueError("find_text cannot be empty")
        result = await self.session.execute(
            select(BrainVaultRecord)
            .where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.namespace.in_(namespaces),
                BrainVaultRecord.deleted_at.is_(None),
            )
            .order_by(BrainVaultRecord.id)
        )
        matches = []
        for record in result.scalars().all():
            title = record.title or ""
            content = record.content or ""
            occurrences = title.count(find_text) + content.count(find_text)
            if occurrences:
                matches.append(
                    {
                        "record_id": record.id,
                        "namespace": record.namespace,
                        "title": record.title,
                        "content": record.content,
                        "version": record.version,
                        "occurrence_count": occurrences,
                    }
                )
        return matches

    @_private_vault_operation
    async def apply_text_replacement(
        self,
        brain: ManagedBrain,
        *,
        namespaces: list[str],
        find_text: str,
        replacement_text: str,
        expected_digest: str,
        maximum_records: int,
    ) -> dict[str, int] | None:
        """Apply a version-checked replacement atomically and rebuild embeddings."""
        import hashlib
        import json

        from app.brain_manager.db_models import BrainVaultRecordVersion

        matches = await self.text_replacement_matches(
            brain, namespaces=namespaces, find_text=find_text
        )
        digest_payload = [
            [
                str(item["record_id"]),
                item["version"],
                item["namespace"],
                item["title"],
                item["content"],
            ]
            for item in matches
        ]
        actual_digest = hashlib.sha256(
            json.dumps(digest_payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if actual_digest != expected_digest or len(matches) > maximum_records:
            return None
        if not matches:
            return {"record_count": 0, "occurrence_count": 0}

        ids = [item["record_id"] for item in matches]
        result = await self.session.execute(
            select(BrainVaultRecord).where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.id.in_(ids),
                BrainVaultRecord.deleted_at.is_(None),
            )
        )
        records = {record.id: record for record in result.scalars().all()}
        if len(records) != len(matches):
            return None

        occurrence_count = 0
        for item in matches:
            record = records[item["record_id"]]
            if record.version != item["version"]:
                return None
            if record.title is not None:
                occurrence_count += record.title.count(find_text)
                record.title = record.title.replace(find_text, replacement_text)
            if record.content is not None:
                occurrence_count += record.content.count(find_text)
                record.content = record.content.replace(find_text, replacement_text)
            metadata = dict(record.record_metadata or {})
            metadata.update(
                {
                    "ai_managed": True,
                    "ai_managed_at": datetime.now(timezone.utc).isoformat(),
                    "ai_last_operation": "bulk_text_replaced",
                }
            )
            record.record_metadata = metadata
            record.version += 1

        await self.session.execute(
            delete(BrainVaultRecordVersion).where(
                BrainVaultRecordVersion.brain_id == brain.brain_id,
                BrainVaultRecordVersion.owner_id == brain.owner_id,
                BrainVaultRecordVersion.record_id.in_(ids),
            )
        )
        await self.session.execute(
            delete(BrainVaultEmbedding).where(
                BrainVaultEmbedding.brain_id == brain.brain_id,
                BrainVaultEmbedding.owner_id == brain.owner_id,
                BrainVaultEmbedding.record_id.in_(ids),
            )
        )
        for item in matches:
            record = records[item["record_id"]]
            indexed_text = record.content or record.title or ""
            chunks = chunk_text(indexed_text)
            vectors = self.embedding_provider.embed(chunks)
            for chunk_index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                self.session.add(
                    BrainVaultEmbedding(
                        brain_id=brain.brain_id,
                        owner_id=brain.owner_id,
                        record_id=record.id,
                        chunk_index=chunk_index,
                        dimension=len(vector),
                        model_id=MODEL_ID,
                        vector=[float(value) for value in vector],
                        content_hash=_content_hash(chunk),
                    )
                )
        await self.session.commit()
        return {
            "record_count": len(matches),
            "occurrence_count": occurrence_count,
        }

    @_private_vault_operation
    async def count_namespace_records(
        self, brain: ManagedBrain, namespace: str
    ) -> int:
        """Count active and soft-deleted records for a namespace purge preview."""
        return int(
            await self.session.scalar(
                select(func.count(BrainVaultRecord.id)).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                    BrainVaultRecord.namespace == namespace,
                )
            )
            or 0
        )

    @_private_vault_operation
    async def namespace_record_ids(
        self, brain: ManagedBrain, namespace: str
    ) -> list[uuid.UUID]:
        """Return the exact record set for a bounded, owner-scoped delete plan."""
        result = await self.session.execute(
            select(BrainVaultRecord.id)
            .where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.namespace == namespace,
            )
            .order_by(BrainVaultRecord.id)
        )
        return list(result.scalars().all())

    @_private_vault_operation
    async def update_record(
        self,
        brain: ManagedBrain,
        record_id: uuid.UUID,
        *,
        expected_version: int,
        title: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        content: str | None = None,
        confirm_original_edit: bool = False,
    ) -> BrainVaultRecord:
        """Apply a version-checked edit; original source text needs confirmation."""
        record = await self.get_record(brain, record_id)
        if record.version != expected_version:
            raise RecordVersionConflict("record version changed")
        if (
            content is not None
            and record.record_metadata.get("is_original")
            and not confirm_original_edit
        ):
            raise PermissionError("editing original content requires owner confirmation")
        protected_metadata = {
            "is_original",
            "source_app",
            "namespace",
            "object_id",
            "checksum_sha256",
            "processing_id",
        }
        if metadata and any(
            key in protected_metadata
            and value != record.record_metadata.get(key)
            for key, value in metadata.items()
        ):
            raise PermissionError("record provenance fields cannot be changed")
        updated_values: dict[str, Any] = {"version": expected_version + 1}
        if title is not None:
            updated_values["title"] = title
        updated_metadata = dict(record.record_metadata or {})
        if summary is not None or metadata is not None:
            if summary is not None:
                updated_metadata["summary"] = summary
            if metadata is not None:
                updated_metadata.update(metadata)
            updated_values["record_metadata"] = updated_metadata
        if content is not None:
            updated_values["content"] = content
        result = await self.session.execute(
            update(BrainVaultRecord)
            .where(
                BrainVaultRecord.id == record_id,
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.version == expected_version,
                BrainVaultRecord.deleted_at.is_(None),
            )
            .values(**updated_values)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            raise RecordVersionConflict("record version changed")
        await self.session.execute(
            delete(BrainVaultRecordVersion).where(
                BrainVaultRecordVersion.brain_id == brain.brain_id,
                BrainVaultRecordVersion.owner_id == brain.owner_id,
                BrainVaultRecordVersion.record_id == record_id,
            )
        )
        await self.session.commit()
        await self.session.refresh(record)
        if content is not None or title is not None:
            await self.index_record(brain, record)
        return record

    @_private_vault_operation
    async def purge_record(self, brain: ManagedBrain, record_id: uuid.UUID) -> bool:
        """Permanently remove one record and its vault-side derived data."""
        try:
            record = await self.get_record(brain, record_id)
        except VaultRecordNotFound:
            return False
        await self.session.execute(
            delete(BrainVaultRelationship).where(
                (BrainVaultRelationship.source_record_id == record.id)
                | (BrainVaultRelationship.target_record_id == record.id)
            )
        )
        await self.session.execute(
            delete(BrainVaultEmbedding).where(
                BrainVaultEmbedding.brain_id == brain.brain_id,
                BrainVaultEmbedding.owner_id == brain.owner_id,
                BrainVaultEmbedding.record_id == record.id,
            )
        )
        await self.session.execute(
            delete(BrainVaultRecordVersion).where(
                BrainVaultRecordVersion.brain_id == brain.brain_id,
                BrainVaultRecordVersion.owner_id == brain.owner_id,
                BrainVaultRecordVersion.record_id == record.id,
            )
        )
        await self.session.delete(record)
        await self.session.commit()
        return True

    @_private_vault_operation
    async def purge_namespace(
        self,
        brain: ManagedBrain,
        namespace: str,
        *,
        expected_count: int | None = None,
        expected_record_digest: str | None = None,
    ) -> int | None:
        """Permanently remove records and derived data from one namespace."""
        result = await self.session.execute(
            select(BrainVaultRecord.id).where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.namespace == namespace,
            )
        )
        record_ids = list(result.scalars().all())
        if expected_count is not None and len(record_ids) != expected_count:
            return None
        if expected_record_digest is not None:
            digest = hashlib.sha256(
                "\n".join(sorted(str(record_id) for record_id in record_ids)).encode(
                    "utf-8"
                )
            ).hexdigest()
            if digest != expected_record_digest:
                return None
        if not record_ids:
            return 0
        await self.session.execute(
            delete(BrainVaultRelationship).where(
                (BrainVaultRelationship.source_record_id.in_(record_ids))
                | (BrainVaultRelationship.target_record_id.in_(record_ids))
            )
        )
        await self.session.execute(
            delete(BrainVaultEmbedding).where(
                BrainVaultEmbedding.brain_id == brain.brain_id,
                BrainVaultEmbedding.owner_id == brain.owner_id,
                BrainVaultEmbedding.record_id.in_(record_ids),
            )
        )
        await self.session.execute(
            delete(BrainVaultRecordVersion).where(
                BrainVaultRecordVersion.brain_id == brain.brain_id,
                BrainVaultRecordVersion.owner_id == brain.owner_id,
                BrainVaultRecordVersion.record_id.in_(record_ids),
            )
        )
        await self.session.execute(
            delete(BrainVaultRecord).where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.namespace == namespace,
                BrainVaultRecord.id.in_(record_ids),
            )
        )
        await self.session.commit()
        return len(record_ids)

    @_private_vault_operation
    async def list_record_versions(
        self, brain: ManagedBrain, record_id: uuid.UUID
    ) -> list[BrainVaultRecordVersion]:
        """List immutable snapshots for one record owned by this Brain."""
        await self.get_record(brain, record_id)
        result = await self.session.execute(
            select(BrainVaultRecordVersion)
            .where(
                BrainVaultRecordVersion.brain_id == brain.brain_id,
                BrainVaultRecordVersion.owner_id == brain.owner_id,
                BrainVaultRecordVersion.record_id == record_id,
            )
            .order_by(BrainVaultRecordVersion.version.desc())
        )
        return list(result.scalars().all())

    @_private_vault_operation
    async def delete_record(self, brain: ManagedBrain, record_id: uuid.UUID) -> bool:
        """Soft-delete one record and remove it from retrieval."""
        try:
            record = await self.get_record(brain, record_id)
        except VaultRecordNotFound:
            return False
        await self.session.execute(
            delete(BrainVaultEmbedding).where(BrainVaultEmbedding.record_id == record.id)
        )
        await self.session.execute(
            delete(BrainVaultRelationship).where(
                (BrainVaultRelationship.source_record_id == record.id)
                | (BrainVaultRelationship.target_record_id == record.id)
            )
        )
        record.deleted_at = utcnow()
        record.version += 1
        await self.session.commit()
        return True

    # -- relationships -----------------------------------------------------
    @_private_vault_operation
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

    @_private_vault_operation
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
    @_private_vault_operation
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

    @_private_vault_operation
    async def semantic_search(
        self,
        brain: ManagedBrain,
        query: str,
        *,
        limit: int = 10,
        min_score: float | None = None,
        namespaces: list[str] | None = None,
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
        filters = [
            BrainVaultEmbedding.brain_id == brain.brain_id,
            BrainVaultEmbedding.owner_id == brain.owner_id,
            BrainVaultRecord.brain_id == brain.brain_id,
            BrainVaultRecord.owner_id == brain.owner_id,
            BrainVaultRecord.deleted_at.is_(None),
        ]
        if namespaces is not None:
            if not namespaces:
                return []
            filters.append(BrainVaultRecord.namespace.in_(namespaces))
        result = await self.session.execute(
            select(BrainVaultEmbedding, BrainVaultRecord)
            .join(
                BrainVaultRecord,
                BrainVaultRecord.id == BrainVaultEmbedding.record_id,
            )
            .where(*filters)
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
                    "namespace": record.namespace,
                    "title": record.title,
                    "source_app": record.source_app,
                    "content": (record.content or "")[:2000],
                    "source_reference": record.source_reference,
                    "metadata": record.record_metadata or {},
                    "version": record.version,
                    "score": round(float(score), 6),
                    "created_at": record.created_at.isoformat()
                    if record.created_at
                    else None,
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(1, limit)]

    @_private_vault_operation
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
                    BrainVaultRecord.deleted_at.is_(None),
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

    @_private_vault_operation
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
