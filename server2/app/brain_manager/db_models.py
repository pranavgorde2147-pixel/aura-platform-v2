"""Persistence models owned by the Server 2 Brain manager.

These tables live in the same metadata database as the object-storage metadata
(``STORAGE_DATABASE_URL``). Server 2 owns them; Server 1 never writes to them
directly - it talks to Server 2 over the service-authenticated API.

Every table that holds user content is keyed by ``brain_id`` *and*
``owner_id``. Both are written once at provisioning time and are never taken
from a request payload, so a job or a request can never re-target another
Brain.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.storage_db import GUID, Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BrainManifestRecord(Base):
    """The persisted resource manifest of one Brain."""

    __tablename__ = "brain_manifests"

    brain_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BrainAppConnection(Base):
    """A Brain-scoped snapshot of one authorized application connection.

    Server 1 stays the source of truth for the app catalog and the user's
    consent. Server 2 keeps this *authorization snapshot* so that vault reads,
    writes and cross-app routing can be authorized at the storage boundary
    without a synchronous call back to Server 1.
    """

    __tablename__ = "brain_app_connections"
    __table_args__ = (
        UniqueConstraint("brain_id", "app_slug", name="uq_brain_app_connections_slug"),
        Index("ix_brain_app_connections_owner", "owner_id", "app_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    app_slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ``[{"scope": "memory", "action": "write"}, ...]`` - the grants the user
    #: actually consented to.
    granted_scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    consent_state: Mapped[str] = mapped_column(String(32), nullable=False, default="granted")
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BrainVaultRecord(Base):
    """One record inside a Brain's personal vault.

    The record *body* lives either inline (relational destinations) or in the
    object store behind ``object_key`` (object-backed destinations). The full
    body is never duplicated into a second table.
    """

    __tablename__ = "brain_vault_records"
    __table_args__ = (
        Index("ix_brain_vault_records_brain_type", "brain_id", "record_type"),
        Index("ix_brain_vault_records_brain_app", "brain_id", "source_app"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    source_app: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    access_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="owner_only")
    record_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrainVaultRelationship(Base):
    """An owner-scoped edge between two vault records (or to an app)."""

    __tablename__ = "brain_vault_relationships"
    __table_args__ = (
        Index("ix_brain_vault_relationships_brain_source", "brain_id", "source_record_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    source_record_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brain_vault_records.id", ondelete="CASCADE"), nullable=False
    )
    target_record_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("brain_vault_records.id", ondelete="CASCADE"), nullable=True
    )
    target_app: Mapped[str | None] = mapped_column(String(128), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BrainVaultEmbedding(Base):
    """One embedding chunk inside a Brain's own vector index.

    Each Brain owns a private vector space: retrieval always filters on
    ``brain_id`` *and* ``owner_id`` so a cross-Brain hit is impossible even if
    the caller guesses a record id.
    """

    __tablename__ = "brain_vault_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "brain_id", "record_id", "chunk_index", name="uq_brain_vault_embeddings_chunk"
        ),
        Index("ix_brain_vault_embeddings_brain", "brain_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    record_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brain_vault_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, default="deterministic-v1")
    vector: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BrainProcessingJob(Base):
    """One queued unit of Brain processing work.

    ``brain_id`` and ``owner_id`` are set when the job is enqueued and are
    **immutable**: the job payload can never re-target a job to another Brain.
    """

    __tablename__ = "brain_processing_jobs"
    __table_args__ = (
        Index("ix_brain_processing_jobs_brain_status", "brain_id", "status"),
        Index("ix_brain_processing_jobs_status_available", "status", "available_at"),
        UniqueConstraint("brain_id", "idempotency_key", name="uq_brain_jobs_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrainUsageRecord(Base):
    """One usage/health record for a Brain."""

    __tablename__ = "brain_usage_records"
    __table_args__ = (Index("ix_brain_usage_records_brain_created", "brain_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BrainConversation(Base):
    """One conversation owned by a Brain."""

    __tablename__ = "brain_conversations"
    __table_args__ = (Index("ix_brain_conversations_brain", "brain_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BrainMessage(Base):
    """One message inside a Brain conversation."""

    __tablename__ = "brain_messages"
    __table_args__ = (Index("ix_brain_messages_conversation", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brain_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BrainRuntimeMetric(Base):
    """Aggregated runtime counters for one Brain."""

    __tablename__ = "brain_runtime_metrics"
    __table_args__ = (UniqueConstraint("brain_id", name="uq_brain_runtime_metrics_brain"),)

    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BrainHealthCheck(Base):
    """The last recorded health probe for a Brain."""

    __tablename__ = "brain_health_checks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_status: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class BrainVectorSpace(Base):
    """The vector-index descriptor of one Brain (dimension, model, state)."""

    __tablename__ = "brain_vector_spaces"
    __table_args__ = (UniqueConstraint("brain_id", name="uq_brain_vector_spaces_brain"),)

    brain_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("managed_brains.brain_id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=256)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, default="deterministic-v1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    retrieval_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


__all__ = [
    "BrainAppConnection",
    "BrainConversation",
    "BrainHealthCheck",
    "BrainManifestRecord",
    "BrainMessage",
    "BrainProcessingJob",
    "BrainRuntimeMetric",
    "BrainUsageRecord",
    "BrainVaultEmbedding",
    "BrainVaultRecord",
    "BrainVaultRelationship",
    "BrainVectorSpace",
]
