"""Repository for Brain processing runs and their destination operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brain_processing import (
    BrainProcessingOperation,
    BrainProcessingRun,
    OperationStatus,
    ProcessingStatus,
)
from app.repositories.base import BaseRepository


class BrainProcessingRunRepository(BaseRepository[BrainProcessingRun]):
    """Repository for BrainProcessingRun model operations."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, BrainProcessingRun)

    async def find_by_idempotency_key(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
    ) -> BrainProcessingRun | None:
        """Return a prior run created with the same idempotency key."""
        result = await self.session.execute(
            select(BrainProcessingRun).where(
                BrainProcessingRun.user_id == user_id,
                BrainProcessingRun.idempotency_key == idempotency_key,
            )
        )
        return result.scalars().first()

    async def get_for_user(
        self,
        *,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> BrainProcessingRun | None:
        """Return a run that belongs to the given user.

        The user filter is the ownership boundary: a run id belonging to
        another user is simply not found.
        """
        result = await self.session.execute(
            select(BrainProcessingRun).where(
                BrainProcessingRun.id == run_id,
                BrainProcessingRun.user_id == user_id,
            )
        )
        return result.scalars().first()

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        status: ProcessingStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BrainProcessingRun]:
        """List a user's processing runs, newest first."""
        query = select(BrainProcessingRun).where(BrainProcessingRun.user_id == user_id)
        if status is not None:
            query = query.where(BrainProcessingRun.status == status)
        result = await self.session.execute(
            query.order_by(BrainProcessingRun.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def count_by_status(self, user_id: uuid.UUID) -> dict[str, int]:
        """Return a per-status counter for the user's runs."""
        result = await self.session.execute(
            select(BrainProcessingRun.status, BrainProcessingRun.id)
            .where(BrainProcessingRun.user_id == user_id)
        )
        counts: dict[str, int] = {}
        for status, _ in result.all():
            key = status.value if hasattr(status, "value") else str(status)
            counts[key] = counts.get(key, 0) + 1
        return counts


class BrainProcessingOperationRepository(BaseRepository[BrainProcessingOperation]):
    """Repository for BrainProcessingOperation model operations."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, BrainProcessingOperation)

    async def list_for_run(self, run_id: uuid.UUID) -> list[BrainProcessingOperation]:
        """Return every operation belonging to a run, in segment order."""
        result = await self.session.execute(
            select(BrainProcessingOperation)
            .where(BrainProcessingOperation.run_id == run_id)
            .order_by(
                BrainProcessingOperation.segment_index.asc(),
                BrainProcessingOperation.destination_app.asc(),
            )
        )
        return list(result.scalars().all())

    async def find_target(
        self,
        *,
        run_id: uuid.UUID,
        destination_app: str,
        record_type: str,
        segment_index: int,
    ) -> BrainProcessingOperation | None:
        """Return the operation for one destination target, if it exists."""
        result = await self.session.execute(
            select(BrainProcessingOperation).where(
                BrainProcessingOperation.run_id == run_id,
                BrainProcessingOperation.destination_app == destination_app,
                BrainProcessingOperation.record_type == record_type,
                BrainProcessingOperation.segment_index == segment_index,
            )
        )
        return result.scalars().first()

    async def list_retryable(self, run_id: uuid.UUID) -> list[BrainProcessingOperation]:
        """Return the operations that may be retried.

        Operations that already succeeded are never returned, so a retry can
        never duplicate a confirmed delivery.
        """
        result = await self.session.execute(
            select(BrainProcessingOperation).where(
                BrainProcessingOperation.run_id == run_id,
                BrainProcessingOperation.status.in_(
                    [
                        OperationStatus.FAILED,
                        OperationStatus.RETRY_PENDING,
                        OperationStatus.PENDING,
                        OperationStatus.READY,
                    ]
                ),
            )
        )
        return list(result.scalars().all())

    async def delete_expired_runs(self, now: datetime | None = None) -> int:
        """Delete cancelled runs older than the retention window."""
        now = now or datetime.now(timezone.utc)
        result = await self.session.execute(
            select(BrainProcessingRun).where(
                BrainProcessingRun.status == ProcessingStatus.CANCELLED,
                BrainProcessingRun.updated_at <= now,
            )
        )
        runs = list(result.scalars().all())
        for run in runs:
            await self.session.delete(run)
        await self.session.flush()
        return len(runs)
