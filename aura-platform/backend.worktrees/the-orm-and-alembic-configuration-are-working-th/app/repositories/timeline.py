"""Timeline repository for database access to timeline entries."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.timeline import Timeline
from app.repositories.base import BaseRepository


class TimelineRepository(BaseRepository[Timeline]):
    """Repository for Timeline model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize TimelineRepository with async session."""
        super().__init__(session, Timeline)

    async def get_by_ai_identity_id(
        self, ai_identity_id: uuid.UUID, limit: int = 100
    ) -> list[Timeline]:
        """Retrieve timeline entries for a specific AI identity."""
        result = await self.session.execute(
            select(Timeline)
            .where(Timeline.ai_identity_id == ai_identity_id)
            .order_by(Timeline.occurred_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_by_event_type(
        self, ai_identity_id: uuid.UUID, event_type: str, limit: int = 50
    ) -> list[Timeline]:
        """Retrieve timeline entries filtered by event type."""
        result = await self.session.execute(
            select(Timeline)
            .where(
                and_(
                    Timeline.ai_identity_id == ai_identity_id,
                    Timeline.event_type == event_type,
                )
            )
            .order_by(Timeline.occurred_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_recent_entries(
        self, ai_identity_id: uuid.UUID, hours: int = 24, limit: int = 100
    ) -> list[Timeline]:
        """Retrieve recent timeline entries within the specified hours."""
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        result = await self.session.execute(
            select(Timeline)
            .where(
                and_(
                    Timeline.ai_identity_id == ai_identity_id,
                    Timeline.occurred_at >= cutoff_time,
                )
            )
            .order_by(Timeline.occurred_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
