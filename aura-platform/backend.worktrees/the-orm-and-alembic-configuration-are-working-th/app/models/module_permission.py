"""Module permission ORM model for user access control."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLAlchemyEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.installed_module import InstalledModule
    from app.models.user import User


class PermissionAction(str, Enum):
    """Supported permission actions for a module."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"


class ModulePermission(BaseModel):
    """Represents explicit permissions granted for a module."""

    __tablename__ = "module_permissions"
    __table_args__ = (
        UniqueConstraint("user_id", "installed_module_id", "scope", name="uq_module_permissions_user_module_scope"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installed_module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installed_modules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope: Mapped[str] = mapped_column(String(100), nullable=False, default="default", index=True)
    permission: Mapped[PermissionAction] = mapped_column(
        SQLAlchemyEnum(PermissionAction, native_enum=False),
        default=PermissionAction.READ,
        nullable=False,
        index=True,
    )

    user: Mapped["User"] = relationship(back_populates="module_permissions", lazy="selectin")
    installed_module: Mapped["InstalledModule"] = relationship(back_populates="module_permissions", lazy="selectin")
