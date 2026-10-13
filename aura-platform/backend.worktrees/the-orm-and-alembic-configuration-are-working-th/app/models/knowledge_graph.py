"""Knowledge graph ORM model for semantic relationships."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity


class KnowledgeGraph(BaseModel):
    """Represents graph ownership and metadata for a user-owned knowledge graph."""

    __tablename__ = "knowledge_graph"

    ai_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )
    graph_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    graph_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False, index=True)

    ai_identity: Mapped["AIIdentity"] = relationship(back_populates="knowledge_graph", lazy="selectin")
