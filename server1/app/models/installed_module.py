"""Installed module ORM model for user module assignments."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum as SQLAlchemyEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.module_permission import ModulePermission


class PlatformType(str, Enum):
    """Supported platform families for modules."""

    AURA = "aura"
    CHRONOS = "chronos"
    SHARED = "shared"


class InstalledModule(BaseModel):
    """Associates a user with a module installed on the platform."""

    __tablename__ = "installed_modules"
    __table_args__ = (
        UniqueConstraint("user_id", "module_slug", name="uq_installed_modules_user_slug"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    module_slug: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    module_name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_type: Mapped[PlatformType] = mapped_column(
        SQLAlchemyEnum(PlatformType, native_enum=False),
        default=PlatformType.AURA,
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    user: Mapped["User"] = relationship(back_populates="installed_modules", lazy="selectin")
    module_permissions: Mapped[list["ModulePermission"]] = relationship(
        back_populates="installed_module",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
