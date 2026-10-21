"""ORM models for the universal Brain processing pipeline.

Two tables back the routing workflow:

``brain_processing_runs``
    One row per submitted input. It carries the *processing state* of the
    input, the idempotency key, provenance references and coarse metadata.
    The raw source content is deliberately **not** stored here: only a bounded
    summary and a pointer to the original input are kept, so the pipeline does
    not become a second copy of every user's private content.

``brain_processing_operations``
    One row per planned destination operation. Each operation is tracked
    independently so a partial delivery can be retried without re-running the
    operations that already succeeded.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SQLAlchemyEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity
    from app.models.user import User


class ProcessingStatus(str, Enum):
    """Lifecycle of one submitted input."""

    RECEIVED = "received"
    VALIDATED = "validated"
    PROCESSING = "processing"
    CLASSIFIED = "classified"
    AWAITING_REVIEW = "awaiting_review"
    READY_FOR_DELIVERY = "ready_for_delivery"
    DELIVERING = "delivering"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERED = "delivered"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationStatus(str, Enum):
    """Lifecycle of one destination operation inside a run."""

    PENDING = "pending"
    READY = "ready"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    AWAITING_REVIEW = "awaiting_review"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BrainProcessingRun(BaseModel):
    """One input submitted to the Brain processing pipeline."""

    __tablename__ = "brain_processing_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_brain_processing_runs_user_idempotency",
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
    # The application that submitted the input. Validated against the
    # registry and the user's connections; never trusted blindly.
    source_app: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Stable client-side identifier for the input (may be supplied by the app).
    input_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    input_type: Mapped[str] = mapped_column(String(64), default="text", nullable=False)
    status: Mapped[ProcessingStatus] = mapped_column(
        SQLAlchemyEnum(ProcessingStatus, native_enum=False),
        default=ProcessingStatus.RECEIVED,
        nullable=False,
        index=True,
    )
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Pointer to the original input (object reference or upstream record id).
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Bounded, non-sensitive description of the input. The full content is
    # never duplicated into this table.
    content_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How the understanding layer produced its labels.
    analysis_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)

    user: Mapped["User"] = relationship(lazy="selectin")
    operations: Mapped[list["BrainProcessingOperation"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="BrainProcessingOperation.segment_index",
    )


class BrainProcessingOperation(BaseModel):
    """One planned or completed destination operation of a processing run."""

    __tablename__ = "brain_processing_operations"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "destination_app",
            "record_type",
            "segment_index",
            name="uq_brain_processing_operations_target",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("brain_processing_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    destination_app: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[OperationStatus] = mapped_column(
        SQLAlchemyEnum(OperationStatus, native_enum=False),
        default=OperationStatus.PENDING,
        nullable=False,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Identifier of the stored record once the operation is confirmed.
    record_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    operation_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["BrainProcessingRun"] = relationship(back_populates="operations", lazy="selectin")
