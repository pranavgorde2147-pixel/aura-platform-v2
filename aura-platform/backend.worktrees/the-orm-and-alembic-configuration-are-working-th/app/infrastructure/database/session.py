"""SQLAlchemy session management for FastAPI dependency injection."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def create_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create an async SQLAlchemy session factory."""
    settings = get_settings()
    database_url = settings.postgres_url

    if not database_url:
        raise ValueError("POSTGRES_URL is not configured")

    async_database_url = database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")

    async_engine = create_async_engine(
        async_database_url,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        echo=settings.debug,
    )

    return async_sessionmaker(bind=async_engine, expire_on_commit=False, autoflush=False, autocommit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for dependency injection."""
    settings = get_settings()
    if not settings.postgres_url:
        raise ValueError("POSTGRES_URL is not configured")

    session_factory = create_async_session_factory()
    async with session_factory() as session:
        yield session
