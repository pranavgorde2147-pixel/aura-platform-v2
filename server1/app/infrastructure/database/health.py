"""Database health-check helpers."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Tables that must exist for authentication and Personal Brain access to work.
# Connectivity alone is not enough: a reachable but unmigrated database answers
# ``SELECT 1`` and then fails every real request (the original production bug).
_REQUIRED_TABLES = (
    "users",
    "ai_identities",
    "personal_ai_brains",
    "sessions",
    "brain_memories",
)


async def check_database_health(session: AsyncSession) -> bool:
    """Verify PostgreSQL connectivity *and* that the core schema is provisioned.

    Returns ``False`` when the database is reachable but required tables are
    missing, so readiness reflects whether the API can actually serve auth and
    brain traffic rather than merely open a connection.
    """
    result = await session.execute(text("SELECT 1"))
    if result.scalar_one() != 1:
        return False

    for table in _REQUIRED_TABLES:
        exists = await session.execute(
            text("SELECT to_regclass(:table_name)"),
            {"table_name": table},
        )
        if exists.scalar_one() is None:
            return False
    return True
