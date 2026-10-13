"""User ORM model for the AI identity foundation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SQLAlchemyEnum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.ai_identity import AIIdentity
from app.models.base import BaseModel
from app.models.device import Device
from app.models.installed_module import InstalledModule
from app.models.knowledge_graph import KnowledgeGraph
from app.models.memory_vault import MemoryVault
from app.models.module_permission import ModulePermission
from app.models.session import Session
from app.models.timeline import Timeline

if TYPE_CHECKING:
    from app.models.ai_identity import AIIdentity
    from app.models.device import Device
    from app.models.installed_module import InstalledModule
    from app.models.knowledge_graph import KnowledgeGraph
    from app.models.memory_vault import MemoryVault
    from app.models.module_permission import ModulePermission
    from app.models.session import Session
    from app.models.timeline import Timeline


class UserStatus(str, Enum):
    """Lifecycle states for a user account."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class User(BaseModel):
    """Represents a human user in the AURA platform."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[UserStatus] = mapped_column(
        SQLAlchemyEnum(UserStatus, native_enum=False),
        default=UserStatus.ACTIVE,
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ai_identity: Mapped["AIIdentity | None"] = relationship(
        "AIIdentity",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
    devices: Mapped[list["Device"]] = relationship(
        "Device",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    sessions: Mapped[list["Session"]] = relationship(
        "Session",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    installed_modules: Mapped[list["InstalledModule"]] = relationship(
        "InstalledModule",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    module_permissions: Mapped[list["ModulePermission"]] = relationship(
        "ModulePermission",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
