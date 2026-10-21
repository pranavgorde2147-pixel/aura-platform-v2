"""Synchronous SQLAlchemy engine configuration for PostgreSQL tooling.

The application itself uses the async engine in :mod:`session`. This sync
engine is provided for scripts/migrations that need a blocking connection and
is created lazily so importing the module has no side effects.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings
from app.infrastructure.database.session import to_sync_url

_engine: Engine | None = None


def create_database_engine() -> Engine:
    """Create a PostgreSQL engine with connection pooling settings."""
    settings = get_settings()
    database_url = settings.postgres_url

    if not database_url:
        raise ValueError("POSTGRES_URL is not configured")

    return create_engine(
        to_sync_url(database_url),
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        echo=settings.debug,
    )


def get_engine() -> Engine:
    """Return a lazily-created, cached synchronous engine."""
    global _engine
    if _engine is None:
        _engine = create_database_engine()
    return _engine
