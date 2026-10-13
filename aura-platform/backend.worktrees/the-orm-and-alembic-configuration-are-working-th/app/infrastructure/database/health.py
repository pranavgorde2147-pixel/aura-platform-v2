"""Database health-check helpers."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def check_database_health(session: AsyncSession) -> bool:
    """Verify connectivity to PostgreSQL by executing a simple query."""
    result = await session.execute(text("SELECT 1"))
    return result.scalar_one() == 1
