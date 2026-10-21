"""SQLAlchemy session management for FastAPI dependency injection.

A single async engine/session factory is cached for the process lifetime. The
previous implementation created a brand new engine on every request which
leaked connection pools. Tests may call :func:`configure_engine` to point the
application at an isolated database.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def to_async_url(database_url: str) -> str:
    """Normalise a PostgreSQL URL to the asyncpg driver used by the app.

    Handles both the modern ``postgresql://`` and the legacy ``postgres://``
    scheme (Render, Heroku) so the async engine never falls back to psycopg2.
    """
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


def to_sync_url(database_url: str) -> str:
    """Normalise a PostgreSQL URL to the psycopg v3 driver used synchronously.

    Alembic and the synchronous engine must never fall back to the default
    ``psycopg2`` DBAPI, which is not installed. Providers (Render, Neon, Heroku)
    hand out ``postgresql://`` or legacy ``postgres://`` URLs, so pin the
    installed psycopg v3 driver instead.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


def configure_engine(database_url: str | None = None) -> AsyncEngine:
    """(Re)configure the process-wide engine. Intended for tests."""
    global _engine, _session_factory
    settings = get_settings()
    resolved = database_url or settings.postgres_url
    if not resolved:
        raise ValueError("POSTGRES_URL is not configured")
    _engine = create_async_engine(
        to_async_url(resolved),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        echo=settings.debug,
    )
    _session_factory = async_sessionmaker(
        bind=_engine, expire_on_commit=False, autoflush=False, autocommit=False
    )
    return _engine


def get_engine() -> AsyncEngine:
    """Return the cached async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = configure_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        configure_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for dependency injection."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
