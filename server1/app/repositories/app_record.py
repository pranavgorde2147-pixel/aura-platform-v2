"""Repository for application-owned records (application storage)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
import re

from sqlalchemy import String, case, cast, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_record import AppRecord, AppRecordLink
from app.repositories.base import BaseRepository

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "do",
    "does",
    "for",
    "how",
    "i",
    "is",
    "me",
    "my",
    "of",
    "the",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


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

    async def get_for_user(
        self, *, user_id: uuid.UUID, record_id: uuid.UUID
    ) -> AppRecord | None:
        """Return one unexpired, stored record owned by the user."""
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(AppRecord).where(
                AppRecord.id == record_id,
                AppRecord.user_id == user_id,
                AppRecord.processing_status == "stored",
                or_(AppRecord.expires_at.is_(None), AppRecord.expires_at > now),
            )
        )
        return result.scalars().first()

    async def find_by_source_reference(
        self,
        *,
        user_id: uuid.UUID,
        source_app: str,
        source_reference: str,
    ) -> AppRecord | None:
        """Find an existing canonical source record without crossing owners."""
        result = await self.session.execute(
            select(AppRecord)
            .where(
                AppRecord.user_id == user_id,
                AppRecord.destination_app == source_app,
                AppRecord.source_reference == source_reference,
                AppRecord.processing_status == "stored",
            )
            .order_by(AppRecord.created_at.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def find_app_link(
        self,
        *,
        user_id: uuid.UUID,
        record_id: uuid.UUID,
        target_app: str,
        relation_type: str,
    ) -> AppRecordLink | None:
        """Find an existing idempotent app-reference link."""
        result = await self.session.execute(
            select(AppRecordLink).where(
                AppRecordLink.user_id == user_id,
                AppRecordLink.record_id == record_id,
                AppRecordLink.target_app == target_app,
                AppRecordLink.relation_type == relation_type,
            )
        )
        return result.scalars().first()

    async def find_record_link(
        self,
        *,
        user_id: uuid.UUID,
        record_id: uuid.UUID,
        related_record_id: uuid.UUID,
        relation_type: str,
    ) -> AppRecordLink | None:
        """Find an existing idempotent record-to-record link."""
        result = await self.session.execute(
            select(AppRecordLink).where(
                AppRecordLink.user_id == user_id,
                AppRecordLink.record_id == record_id,
                AppRecordLink.related_record_id == related_record_id,
                AppRecordLink.relation_type == relation_type,
            )
        )
        return result.scalars().first()

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        source_apps: set[str],
        record_type: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        target_app: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AppRecord]:
        """List records restricted to explicitly readable source apps."""
        if not source_apps:
            return []
        now = datetime.now(timezone.utc)
        query = select(AppRecord).where(
            AppRecord.user_id == user_id,
            AppRecord.destination_app.in_(source_apps),
            AppRecord.processing_status == "stored",
            or_(AppRecord.expires_at.is_(None), AppRecord.expires_at > now),
        )
        if record_type:
            query = query.where(AppRecord.record_type == record_type)
        if category:
            query = query.where(cast(AppRecord.categories, String).ilike(f'%"{category}"%'))
        if topic:
            query = query.where(cast(AppRecord.topics, String).ilike(f'%"{topic}"%'))
        if occurred_after:
            query = query.where(AppRecord.occurred_at >= occurred_after)
        if occurred_before:
            query = query.where(AppRecord.occurred_at <= occurred_before)
        if target_app:
            linked_record_ids = select(AppRecordLink.record_id).where(
                AppRecordLink.user_id == user_id,
                AppRecordLink.target_app == target_app,
            )
            query = query.where(
                or_(
                    AppRecord.destination_app == target_app,
                    AppRecord.id.in_(linked_record_ids),
                )
            )
        result = await self.session.execute(
            query.order_by(AppRecord.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def search_for_user(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int = 5,
        source_apps: set[str] | None = None,
        record_type: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[AppRecord]:
        """Search canonical relational records for the owner across app sources."""
        terms = list(
            dict.fromkeys(
                word
                for word in re.findall(r"[\w'-]+", query.casefold())
                if len(word) > 1 and word not in _QUERY_STOPWORDS
            )
        )[:12]
        if not terms:
            return []

        if source_apps is not None and not source_apps:
            return []

        term_conditions = []
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            term_conditions.append(
                or_(
                    AppRecord.content.ilike(pattern, escape="\\"),
                    AppRecord.summary.ilike(pattern, escape="\\"),
                    AppRecord.title.ilike(pattern, escape="\\"),
                    cast(AppRecord.topics, String).ilike(pattern, escape="\\"),
                    cast(AppRecord.record_metadata, String).ilike(pattern, escape="\\"),
                )
            )

        relevance = sum(
            (case((condition, 1), else_=0) for condition in term_conditions),
            start=0,
        )
        now = datetime.now(timezone.utc)
        filters = [
            AppRecord.user_id == user_id,
            AppRecord.processing_status == "stored",
            or_(AppRecord.expires_at.is_(None), AppRecord.expires_at > now),
            or_(*term_conditions),
        ]
        if source_apps is not None:
            filters.append(AppRecord.destination_app.in_(source_apps))
        if record_type:
            filters.append(AppRecord.record_type == record_type)
        if category:
            filters.append(cast(AppRecord.categories, String).ilike(f'%"{category}"%'))
        if topic:
            filters.append(cast(AppRecord.topics, String).ilike(f'%"{topic}"%'))
        if occurred_after:
            filters.append(AppRecord.occurred_at >= occurred_after)
        if occurred_before:
            filters.append(AppRecord.occurred_at <= occurred_before)
        result = await self.session.execute(
            select(AppRecord)
            .where(*filters)
            .order_by(relevance.desc(), AppRecord.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def links_for_record(
        self, *, user_id: uuid.UUID, record_id: uuid.UUID
    ) -> list[AppRecordLink]:
        """Return only links owned by the record's user."""
        result = await self.session.execute(
            select(AppRecordLink)
            .where(
                AppRecordLink.user_id == user_id,
                AppRecordLink.record_id == record_id,
            )
            .order_by(AppRecordLink.created_at.asc())
        )
        return list(result.scalars().all())

    async def delete_links_for_record(
        self, *, user_id: uuid.UUID, record_id: uuid.UUID
    ) -> None:
        """Remove owned links that point from or to a record being deleted."""
        await self.session.execute(
            delete(AppRecordLink).where(
                AppRecordLink.user_id == user_id,
                or_(
                    AppRecordLink.record_id == record_id,
                    AppRecordLink.related_record_id == record_id,
                ),
            )
        )

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
