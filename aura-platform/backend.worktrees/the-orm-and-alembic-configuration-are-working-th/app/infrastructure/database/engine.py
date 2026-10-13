"""SQLAlchemy engine configuration for PostgreSQL."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings


def create_database_engine() -> Engine:
    """Create a PostgreSQL engine with connection pooling settings."""
    settings = get_settings()
    database_url = settings.postgres_url

    if not database_url:
        raise ValueError("POSTGRES_URL is not configured")

    return create_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        echo=settings.debug,
    )


engine = create_database_engine()
