"""Personal AI brain ORM model for an AI identity."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SQLAlchemyEnum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity


class BrainStatus(str, Enum):
    """Status values for a personal AI brain."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"


class PersonalAIBrain(BaseModel):
    """Represents the personal AI brain orchestration layer for an identity."""

    __tablename__ = "personal_ai_brains"
    __table_args__ = (
        UniqueConstraint("ai_identity_id", "version", name="uq_personal_ai_brains_identity_version"),
    )

    ai_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )
    brain_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0", index=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    sync_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provisioning_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    resource_manifest_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    model_configuration: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    inference_runtime_status: Mapped[str] = mapped_column(
        String(32), default="unregistered", nullable=False, index=True
    )
    # Identifier of the Brain's dedicated LLM runtime in Server 2. It is unique
    # across all Brains; two Brains never share one runtime id.
    llm_runtime_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Honest isolation level reported by Server 2 (dedicated_context,
    # dedicated_weights, context_isolated_shared_weights or simulated).
    runtime_isolation: Mapped[str | None] = mapped_column(String(48), nullable=True)
    compute_allocation: Mapped[dict[str, int | str]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    storage_resource_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provisioning_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    inference_request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[BrainStatus] = mapped_column(
        SQLAlchemyEnum(BrainStatus, native_enum=False),
        default=BrainStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    ai_identity: Mapped["AIIdentity"] = relationship("AIIdentity", back_populates="personal_ai_brain", lazy="selectin")
