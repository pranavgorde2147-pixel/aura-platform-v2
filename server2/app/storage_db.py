"""Database setup and a portable GUID type for Server 2 object storage.

The metadata store defaults to SQLite (via aiosqlite) so the storage engine is
self-contained. Set ``STORAGE_DATABASE_URL`` to a PostgreSQL URL in production.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import (
    DateTime,
    Index,
    JSON,
    String,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.storage_config import get_settings


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the parent directory for a local SQLite file (e.g. ./data)."""
    if not database_url.startswith("sqlite"):
        return
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url[len(prefix):]
    if raw_path in {":memory:", ":memory:?cache=shared"}:
        return
    path = Path(raw_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


class GUID(TypeDecorator):
    """Platform-independent UUID type stored as CHAR(36) on non-Postgres."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID

            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class Base(DeclarativeBase):
    """Declarative base for storage metadata models."""


class ManagedBrain(Base):
    """Server 2 registry for service-provisioned, owner-isolated Brain spaces.

    This table is the **authoritative** registry: it decides who owns which
    Brain and what state that Brain's resources are in. ``llm_runtime_id`` is
    the identity of the Brain's dedicated inference runtime; it is unique and
    never shared between two Brains.
    """

    __tablename__ = "managed_brains"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_managed_brains_owner"),
        UniqueConstraint("ai_identity_id", name="uq_managed_brains_identity"),
        # A dedicated runtime belongs to exactly one Brain. The index name is
        # shared with the migration runner so both paths create the same
        # constraint (the runner is then a no-op on a fresh database).
        Index("uq_managed_brains_llm_runtime_id", "llm_runtime_id", unique=True),
        Index("ix_managed_brains_llm_runtime_status", "llm_runtime_status"),
    )

    brain_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    ai_identity_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    provisioning_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    resource_manifest_version: Mapped[int] = mapped_column(nullable=False, default=1)
    model_configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Identifier of the Brain's dedicated LLM runtime. Unique per Brain.
    llm_runtime_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_runtime_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unregistered"
    )
    #: How isolated the runtime really is (dedicated / shared-weights / simulated).
    runtime_isolation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_configuration_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compute_allocation: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    storage_resource_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_resource_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_resource_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vault_resource_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_root: Mapped[str | None] = mapped_column(String(512), nullable=True)
    manifest_version: Mapped[int] = mapped_column(nullable=False, default=1)
    connected_app_count: Mapped[int] = mapped_column(nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioning_error: Mapped[str | None] = mapped_column(String(500))
    runtime_last_error: Mapped[str | None] = mapped_column(String(500))
    runtime_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
#: The engine whose schema is known to be up to date.
_schema_ready_engine = None
_schema_lock = None


def get_engine():
    """Return the lazily-created async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_parent_dir(settings.resolved_database_url)
        _engine = create_async_engine(settings.resolved_database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the lazily-created session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


async def init_models() -> None:
    """Create tables if they do not exist and apply pending schema migrations.

    ``create_all`` handles brand-new databases. The migration runner then
    upgrades databases that were created by an older Server 2 build (it adds
    the Brain registry columns, drops the retired ``runtime_status`` column and
    backfills the new columns).
    """
    global _schema_ready_engine
    # Importing the module registers the Brain manager tables on ``Base``.
    from app.brain_manager import db_models as _brain_models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.brain_manager.migrations import run_migrations

        await run_migrations(conn)
    _schema_ready_engine = engine


async def ensure_schema() -> None:
    """Make sure the metadata schema exists before it is queried.

    The startup hook normally creates the tables, but a process that is
    embedded (a test client that never fires startup events, a worker that is
    imported directly) would otherwise fail with "no such table". This helper
    runs the initialisation exactly once per engine.
    """
    global _schema_lock
    engine = get_engine()
    if _schema_ready_engine is engine:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ready_engine is engine:
            return
        await init_models()


async def dispose_engine() -> None:
    """Dispose of the engine during shutdown."""
    global _engine, _session_factory, _schema_ready_engine
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _schema_ready_engine = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    await ensure_schema()
    factory = get_session_factory()
    async with factory() as session:
        yield session
