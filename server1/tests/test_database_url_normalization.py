"""Regression tests for database URL normalisation.

The production registration/login HTTP 500 was caused by the container running
``alembic upgrade head`` with a bare ``postgresql://`` URL. SQLAlchemy selects
the default ``psycopg2`` DBAPI for that URL, which is not an installed
dependency, so the migration crashed before creating any tables.

These tests pin the URL-normalisation helpers that select the installed drivers
(psycopg v3 for Alembic/sync, asyncpg for the app) so a regression cannot bring
back the "reachable database, missing schema" failure.
"""

from __future__ import annotations

import pytest

from app.infrastructure.database.session import to_async_url, to_sync_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+asyncpg://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+psycopg://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
    ],
)
def test_to_sync_url_pins_psycopg_v3(raw, expected):
    assert to_sync_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        ("postgresql+psycopg://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        ("postgresql+asyncpg://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    ],
)
def test_to_async_url_pins_asyncpg(raw, expected):
    assert to_async_url(raw) == expected


def test_non_postgres_urls_are_left_untouched():
    # SQLite (used by local tooling) must not be rewritten.
    assert to_sync_url("sqlite:///./local.db") == "sqlite:///./local.db"
    assert to_async_url("sqlite+aiosqlite:///./local.db") == "sqlite+aiosqlite:///./local.db"
