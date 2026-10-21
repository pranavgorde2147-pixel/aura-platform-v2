"""ORM model for application-owned records.

This table is the **application storage** side of the platform, deliberately
separate from ``brain_memories``:

* ``brain_memories`` holds short, user-specific context used for future
  reasoning (facts, preferences, short index lines).
* ``app_records`` holds the *canonical records of an application*: lecture
  notes, meeting transcripts, diary entries, tasks, trips and documents.

Records are partitioned by ``destination_app`` and ``record_type`` so each
application keeps its own logical record space, and every row preserves its
owner, its provenance (the processing run and segment it came from), its
retention policy and its processing status.

Full source content is stored **once**: when a destination is backed by the
object storage API hosted on Server 2, the body lives in the object store and
``app_records`` keeps a pointer (``storage_type="object"``) instead of a
second copy of the bytes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity
    from app.models.brain_processing import BrainProcessingRun
    from app.models.user import User


class AppRecord(BaseModel):
    """A permanent record owned by one application for one user."""

    __tablename__ = "app_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "destination_app",
            "record_type",
            "dedupe_key",
            name="uq_app_records_user_destination_type_dedupe",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ai_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_identities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    destination_app: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full body, kept inline only for relational destinations. Object-backed
    # destinations store the body in the object store and leave this NULL.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_type: Mapped[str] = mapped_column(String(32), default="relational", nullable=False)
    object_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    # Provenance: the run and segment this record was produced from.
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain_processing_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_segment_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    record_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    access_policy: Mapped[str] = mapped_column(
        String(32), default="owner_only", nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retention_policy: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    processing_status: Mapped[str] = mapped_column(
        String(32), default="stored", nullable=False, index=True
    )

    user: Mapped["User"] = relationship(lazy="selectin")
    source_run: Mapped["BrainProcessingRun | None"] = relationship(lazy="selectin")


class AppRecordLink(BaseModel):
    """Owner-scoped relationship or app reference for a canonical record."""

    __tablename__ = "app_record_links"
    __table_args__ = (
        CheckConstraint(
            "(related_record_id IS NULL) <> (target_app IS NULL)",
            name="ck_app_record_links_one_target",
        ),
        CheckConstraint(
            "related_record_id IS NULL OR related_record_id <> record_id",
            name="ck_app_record_links_no_self_reference",
        ),
        Index(
            "uq_app_record_links_target_app_relationship",
            "user_id",
            "record_id",
            "target_app",
            "relation_type",
            unique=True,
            postgresql_where=text("related_record_id IS NULL"),
            sqlite_where=text("related_record_id IS NULL"),
        ),
        Index(
            "uq_app_record_links_related_record_relationship",
            "user_id",
            "record_id",
            "related_record_id",
            "relation_type",
            unique=True,
            postgresql_where=text("target_app IS NULL"),
            sqlite_where=text("target_app IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    related_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app_records.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    target_app: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)

    user: Mapped["User"] = relationship(lazy="selectin")
