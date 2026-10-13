"""Device ORM model for user-owned devices."""

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
    from app.models.user import User


class DeviceType(str, Enum):
    """Supported device categories."""

    MOBILE = "mobile"
    DESKTOP = "desktop"
    TABLET = "tablet"
    WATCH = "watch"


class DevicePlatform(str, Enum):
    """Supported client platforms."""

    IOS = "ios"
    ANDROID = "android"
    WEB = "web"
    DESKTOP = "desktop"


class Device(BaseModel):
    """Represents a device associated with a user."""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("device_uuid", name="uq_devices_device_uuid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
        unique=True,
        index=True,
    )
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    device_type: Mapped[DeviceType] = mapped_column(
        SQLAlchemyEnum(DeviceType, native_enum=False),
        default=DeviceType.DESKTOP,
        nullable=False,
        index=True,
    )
    platform: Mapped[DevicePlatform] = mapped_column(
        SQLAlchemyEnum(DevicePlatform, native_enum=False),
        default=DevicePlatform.WEB,
        nullable=False,
        index=True,
    )
    app_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    push_token: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    user: Mapped["User"] = relationship(back_populates="devices", lazy="selectin")
