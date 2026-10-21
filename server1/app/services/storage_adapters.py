"""Storage adapters that write records into a destination application.

A destination application declares how its records are stored in the registry
(``storage_adapter``):

``relational``
    The record body is stored in the ``app_records`` table, partitioned by
    destination application and record type.

``object``
    The record body is stored once in the object storage service (Server 3,
    hosted inside Server 2) and ``app_records`` keeps only the metadata and a
    pointer to the object. The full content is therefore never duplicated.

Both adapters are idempotent: the same ``dedupe_key`` for the same
``(user, destination, record type)`` always resolves to the same stored
record, so a retried delivery cannot create a duplicate.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.storage_client import StorageServiceError, get_storage_client
from app.models.app_record import AppRecord
from app.repositories.app_record import AppRecordRepository
from app.services.classification_policy import ClassificationPolicy

logger = logging.getLogger(__name__)


class StorageAdapterError(Exception):
    """Raised when a record could not be persisted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RecordPayload:
    """Everything needed to persist one record in a destination."""

    user_id: uuid.UUID
    destination_app: str
    record_type: str
    content: str
    dedupe_key: str
    title: str | None = None
    summary: str | None = None
    ai_identity_id: uuid.UUID | None = None
    source_run_id: uuid.UUID | None = None
    source_segment_index: int | None = None
    source_reference: str | None = None
    occurred_at: datetime | None = None
    retention_policy: str = "default"


@dataclass
class StoredRecord:
    """The outcome of a successful write."""

    record_id: uuid.UUID
    storage_type: str
    object_reference: str | None = None
    created: bool = True


class RecordStore:
    """Base interface for destination storage adapters."""

    storage_type = "relational"

    async def write(self, payload: RecordPayload) -> StoredRecord:
        raise NotImplementedError

    def _expiry(self, policy: ClassificationPolicy, retention_policy: str) -> datetime | None:
        """Compute the expiry timestamp for a stored record."""
        days = policy.record_retention_days
        if retention_policy == "ephemeral":
            days = min(days, 1) if days else 1
        if days <= 0:
            return None
        return datetime.now(timezone.utc) + timedelta(days=days)


class RelationalRecordStore(RecordStore):
    """Stores the full record body in the relational ``app_records`` table."""

    storage_type = "relational"

    def __init__(self, session: AsyncSession, repository: AppRecordRepository) -> None:
        self.session = session
        self.repository = repository

    async def write(self, payload: RecordPayload) -> StoredRecord:
        existing = await self.repository.find_by_dedupe(
            user_id=payload.user_id,
            destination_app=payload.destination_app,
            record_type=payload.record_type,
            dedupe_key=payload.dedupe_key,
        )
        if existing is not None:
            return StoredRecord(
                record_id=existing.id,
                storage_type=existing.storage_type,
                object_reference=existing.object_reference,
                created=False,
            )

        record = AppRecord(
            user_id=payload.user_id,
            ai_identity_id=payload.ai_identity_id,
            destination_app=payload.destination_app,
            record_type=payload.record_type,
            title=payload.title,
            summary=payload.summary,
            content=payload.content,
            storage_type="relational",
            source_run_id=payload.source_run_id,
            source_segment_index=payload.source_segment_index,
            source_reference=payload.source_reference,
            dedupe_key=payload.dedupe_key,
            occurred_at=payload.occurred_at,
            retention_policy=payload.retention_policy,
            processing_status="stored",
        )
        self.session.add(record)
        await self.session.flush()
        return StoredRecord(record_id=record.id, storage_type="relational", created=True)


class ObjectRecordStore(RecordStore):
    """Stores the record body in the object storage service.

    The object is uploaded once per ``dedupe_key``. A duplicate upload is
    detected by checking the metadata row first, so a retried delivery does
    not create a second object.
    """

    storage_type = "object"

    def __init__(
        self,
        session: AsyncSession,
        repository: AppRecordRepository,
        storage_client=None,
    ) -> None:
        self.session = session
        self.repository = repository
        self._storage_client = storage_client

    @property
    def storage_client(self):
        if self._storage_client is None:
            self._storage_client = get_storage_client()
        return self._storage_client

    async def write(self, payload: RecordPayload) -> StoredRecord:
        existing = await self.repository.find_by_dedupe(
            user_id=payload.user_id,
            destination_app=payload.destination_app,
            record_type=payload.record_type,
            dedupe_key=payload.dedupe_key,
        )
        if existing is not None:
            return StoredRecord(
                record_id=existing.id,
                storage_type=existing.storage_type,
                object_reference=existing.object_reference,
                created=False,
            )

        filename = self._filename(payload)
        try:
            uploaded = await self.storage_client.upload(
                owner_id=str(payload.user_id),
                filename=filename,
                content=payload.content.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                ai_identity_id=str(payload.ai_identity_id) if payload.ai_identity_id else None,
                source_app=payload.destination_app,
            )
        except StorageServiceError as exc:
            logger.error("Object upload failed for %s: %s", payload.destination_app, exc)
            raise StorageAdapterError(
                "storage_unavailable",
                "The object storage service is unavailable",
            ) from exc

        object_id = uploaded.get("id")
        if not object_id:
            raise StorageAdapterError(
                "storage_invalid_response",
                "The object storage service returned an unexpected response",
            )

        record = AppRecord(
            user_id=payload.user_id,
            ai_identity_id=payload.ai_identity_id,
            destination_app=payload.destination_app,
            record_type=payload.record_type,
            title=payload.title,
            summary=payload.summary,
            content=None,
            storage_type="object",
            object_reference=str(object_id),
            source_run_id=payload.source_run_id,
            source_segment_index=payload.source_segment_index,
            source_reference=payload.source_reference,
            dedupe_key=payload.dedupe_key,
            occurred_at=payload.occurred_at,
            retention_policy=payload.retention_policy,
            processing_status="stored",
        )
        self.session.add(record)
        await self.session.flush()
        return StoredRecord(
            record_id=record.id,
            storage_type="object",
            object_reference=str(object_id),
            created=True,
        )

    @staticmethod
    def _filename(payload: RecordPayload) -> str:
        """Build a deterministic, collision-free object name."""
        digest = hashlib.sha256(payload.dedupe_key.encode("utf-8")).hexdigest()[:16]
        return f"{payload.destination_app}/{payload.record_type}/{digest}.txt"


def build_record_store(
    session: AsyncSession,
    adapter: str,
    repository: AppRecordRepository,
    storage_client=None,
) -> RecordStore:
    """Return the record store that matches a registry ``storage_adapter``."""
    if adapter == "object":
        return ObjectRecordStore(session, repository, storage_client=storage_client)
    return RelationalRecordStore(session, repository)


def build_dedupe_key(
    *,
    user_id: uuid.UUID,
    destination_app: str,
    record_type: str,
    run_id: uuid.UUID | None,
    segment_index: int | None,
    content: str,
) -> str:
    """Build the idempotency key for one record.

    The key combines the owning user, the destination, the record type and
    the source provenance, so the same content delivered twice for the same
    segment resolves to one record while two different runs stay separate.
    """
    parts = [
        str(user_id),
        destination_app,
        record_type,
        str(run_id) if run_id else "",
        str(segment_index) if segment_index is not None else "",
        hashlib.sha256(content.encode("utf-8")).hexdigest()[:32],
    ]
    return ":".join(parts)
