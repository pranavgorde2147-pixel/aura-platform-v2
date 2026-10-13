"""Memory vault ORM model for storing user memory fragments."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity


class MemoryVault(BaseModel):
    """Stores metadata for externally referenced memory objects."""

    __tablename__ = "memory_vault"

    ai_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )
    object_reference: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    storage_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    encryption_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)

    ai_identity: Mapped["AIIdentity"] = relationship(back_populates="memory_vault", lazy="selectin")
