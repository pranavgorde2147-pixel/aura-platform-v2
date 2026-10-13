"""Personal AI brain ORM model for an AI identity."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SQLAlchemyEnum, ForeignKey, Integer, String, UniqueConstraint
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
    status: Mapped[BrainStatus] = mapped_column(
        SQLAlchemyEnum(BrainStatus, native_enum=False),
        default=BrainStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    ai_identity: Mapped["AIIdentity"] = relationship("AIIdentity", back_populates="personal_ai_brain", lazy="selectin")
