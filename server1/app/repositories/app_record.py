"""Repository for application-owned records (application storage)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_record import AppRecord
from app.repositories.base import BaseRepository


class AppRecordRepository(BaseRepository[AppRecord]):
    """Repository for AppRecord model operations."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, AppRecord)

    async def find_by_dedupe(
        self,
        *,
        user_id: uuid.UUID,
        destination_app: str,
        record_type: str,
        dedupe_key: str,
    ) -> AppRecord | None:
        """Return an existing record with the same idempotency key."""
        result = await self.session.execute(
            select(AppRecord).where(
                AppRecord.user_id == user_id,
                AppRecord.destination_app == destination_app,
                AppRecord.record_type == record_type,
                AppRecord.dedupe_key == dedupe_key,
            )
        )
        return result.scalars().first()

    async def list_by_destination(
        self,
        *,
        user_id: uuid.UUID,
        destination_app: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AppRecord]:
        """List a user's application records, optionally for one destination."""
        query = select(AppRecord).where(AppRecord.user_id == user_id)
        if destination_app:
            query = query.where(AppRecord.destination_app == destination_app)
        result = await self.session.execute(
            query.order_by(AppRecord.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_run(self, run_id: uuid.UUID) -> list[AppRecord]:
        """Return every record produced by one processing run."""
        result = await self.session.execute(
            select(AppRecord)
            .where(AppRecord.source_run_id == run_id)
            .order_by(AppRecord.created_at.asc())
        )
        return list(result.scalars().all())

    async def delete_expired(self, now: datetime | None = None) -> int:
        """Delete records whose retention window has elapsed."""
        from datetime import timezone

        now = now or datetime.now(timezone.utc)
        result = await self.session.execute(
            select(AppRecord).where(
                AppRecord.expires_at.is_not(None),
                AppRecord.expires_at <= now,
            )
        )
        records = list(result.scalars().all())
        for record in records:
            await self.session.delete(record)
        await self.session.flush()
        return len(records)
