"""AI identity ORM model for the AURA platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SQLAlchemyEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.knowledge_graph import KnowledgeGraph
    from app.models.memory_vault import MemoryVault
    from app.models.personal_ai_brain import PersonalAIBrain
    from app.models.timeline import Timeline
    from app.models.user import User


class BrainStatus(str, Enum):
    """Lifecycle states for an AI identity brain."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"


class AIIdentity(BaseModel):
    """Represents an AI identity belonging to a user."""

    __tablename__ = "ai_identities"
    __table_args__ = (
        UniqueConstraint("ai_uuid", name="uq_ai_identities_ai_uuid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )
    ai_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
        unique=True,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[BrainStatus] = mapped_column(
        SQLAlchemyEnum(BrainStatus, native_enum=False),
        default=BrainStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="ai_identity", lazy="selectin")
    personal_ai_brain: Mapped["PersonalAIBrain | None"] = relationship(
        "PersonalAIBrain",
        back_populates="ai_identity",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    memory_vault: Mapped["MemoryVault | None"] = relationship(
        "MemoryVault",
        back_populates="ai_identity",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    knowledge_graph: Mapped["KnowledgeGraph | None"] = relationship(
        "KnowledgeGraph",
        back_populates="ai_identity",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    timeline_entries: Mapped[list["Timeline"]] = relationship(
        "Timeline",
        back_populates="ai_identity",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
