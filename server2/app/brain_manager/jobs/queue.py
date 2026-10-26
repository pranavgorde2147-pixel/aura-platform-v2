"""The per-Brain processing job queue.

Every job carries the ``brain_id`` and ``owner_id`` of the Brain that created
it. Those two values are written once, when the job is enqueued, and are never
read back out of the job payload - so a crafted payload cannot re-target a job
at another user's Brain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from functools import wraps
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import BrainProcessingJob
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.storage_db import ManagedBrain

#: Payload keys a caller may never set: they belong to the registry.
RESERVED_PAYLOAD_KEYS = frozenset(
    {"brain_id", "owner_id", "owner_user_id", "ai_identity_id"}
)

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_DEAD = "dead"


def _private_job_operation(method):
    """Run job payload and result operations inside the owning Brain's vault."""
    @wraps(method)
    async def wrapped(self, brain: ManagedBrain, *args, **kwargs):
        if self._active_session.get() is not None:
            return await method(self, brain, *args, **kwargs)
        async with get_brain_vault_storage().session(brain) as private_session:
            token = self._active_session.set(private_session)
            try:
                return await method(self, brain, *args, **kwargs)
            finally:
                self._active_session.reset(token)

    return wrapped


class JobQueueError(Exception):
    """Raised when a job cannot be queued or claimed."""


class JobQueueFullError(JobQueueError):
    """Raised when the Brain already has the maximum number of queued jobs."""


@dataclass
class JobEnvelope:
    """One queued job."""

    id: uuid.UUID
    brain_id: uuid.UUID
    owner_id: uuid.UUID
    job_type: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime | None = None

    @property
    def brain_context(self) -> dict[str, Any]:
        """Return the immutable context a handler is allowed to see."""
        return {
            "brain_id": str(self.brain_id),
            "owner_id": str(self.owner_id),
            "job_id": str(self.id),
            "job_type": self.job_type,
        }


def _sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Remove (and reject) reserved keys from a job payload."""
    cleaned = dict(payload or {})
    for key in RESERVED_PAYLOAD_KEYS:
        cleaned.pop(key, None)
    return cleaned


class BrainJobQueue:
    """Queue, claim and complete Brain processing jobs."""

    def __init__(self, session: AsyncSession, *, max_jobs_per_brain: int = 64) -> None:
        self._registry_session = session
        self._active_session: ContextVar[AsyncSession | None] = ContextVar(
            f"brain_job_session_{id(self)}", default=None
        )
        self.max_jobs_per_brain = max_jobs_per_brain

    @property
    def session(self) -> AsyncSession:
        session = self._active_session.get() or self._registry_session
        return session

    @_private_job_operation
    async def enqueue(
        self,
        brain: ManagedBrain,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        available_at: datetime | None = None,
    ) -> BrainProcessingJob:
        """Queue one job for ``brain``.

        The Brain and owner ids come from the registry row, never from the
        payload.
        """
        queued = int(
            await self.session.scalar(
                select(func.count(BrainProcessingJob.id)).where(
                    BrainProcessingJob.brain_id == brain.brain_id,
                    BrainProcessingJob.status.in_((JOB_QUEUED, JOB_RUNNING)),
                )
            )
            or 0
        )
        if queued >= self.max_jobs_per_brain:
            raise JobQueueFullError(
                f"Brain already has {queued} jobs in flight (limit {self.max_jobs_per_brain})"
            )

        job = BrainProcessingJob(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            job_type=job_type,
            status=JOB_QUEUED,
            priority=priority,
            payload=_sanitize_payload(payload),
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            available_at=available_at or datetime.now(timezone.utc),
        )
        self.session.add(job)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            if idempotency_key:
                existing = await self.find_by_idempotency_key(brain, idempotency_key)
                if existing is not None:
                    return existing
            raise
        await self.session.refresh(job)
        return job

    @_private_job_operation
    async def find_by_idempotency_key(
        self, brain: ManagedBrain, idempotency_key: str
    ) -> BrainProcessingJob | None:
        result = await self.session.execute(
            select(BrainProcessingJob).where(
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.idempotency_key == idempotency_key,
            )
        )
        return result.scalars().first()

    @_private_job_operation
    async def claim_next(self, brain: ManagedBrain) -> BrainProcessingJob | None:
        """Claim the next runnable job of one Brain."""
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(BrainProcessingJob)
            .where(
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.owner_id == brain.owner_id,
                BrainProcessingJob.status == JOB_QUEUED,
                BrainProcessingJob.available_at <= now,
            )
            .order_by(
                BrainProcessingJob.priority.asc(), BrainProcessingJob.created_at.asc()
            )
            .limit(1)
        )
        job = result.scalars().first()
        if job is None:
            return None
        await self.session.execute(
            update(BrainProcessingJob)
            .where(
                BrainProcessingJob.id == job.id,
                BrainProcessingJob.status == JOB_QUEUED,
            )
            .values(status=JOB_RUNNING, started_at=now, attempts=BrainProcessingJob.attempts + 1)
        )
        await self.session.commit()
        await self.session.refresh(job)
        return job

    @_private_job_operation
    async def complete(
        self,
        brain: ManagedBrain,
        job: BrainProcessingJob,
        *,
        result_payload: dict[str, Any] | None = None,
    ) -> BrainProcessingJob:
        """Mark one job as completed."""
        result = await self.session.execute(
            select(BrainProcessingJob).where(
                BrainProcessingJob.id == job.id,
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.owner_id == brain.owner_id,
            )
        )
        persisted = result.scalars().first()
        if persisted is None:
            raise JobQueueError("Job does not belong to this Brain")
        persisted.status = JOB_COMPLETED
        persisted.result = result_payload or {}
        persisted.finished_at = datetime.now(timezone.utc)
        persisted.error_code = None
        await self.session.commit()
        await self.session.refresh(persisted)
        return persisted

    @_private_job_operation
    async def fail(
        self,
        brain: ManagedBrain,
        job: BrainProcessingJob,
        *,
        error_code: str,
        retryable: bool = True,
    ) -> BrainProcessingJob:
        """Mark one job as failed (or requeue it for a later attempt)."""
        result = await self.session.execute(
            select(BrainProcessingJob).where(
                BrainProcessingJob.id == job.id,
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.owner_id == brain.owner_id,
            )
        )
        persisted = result.scalars().first()
        if persisted is None:
            raise JobQueueError("Job does not belong to this Brain")
        persisted.error_code = error_code
        persisted.finished_at = datetime.now(timezone.utc)
        if retryable and persisted.attempts < persisted.max_attempts:
            persisted.status = JOB_QUEUED
            persisted.finished_at = None
            persisted.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(60, 2**persisted.attempts))
        else:
            persisted.status = JOB_FAILED if not retryable else JOB_DEAD
        await self.session.commit()
        await self.session.refresh(persisted)
        return persisted

    @_private_job_operation
    async def list_jobs(
        self,
        brain: ManagedBrain,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BrainProcessingJob], int]:
        """List the jobs of one Brain."""
        filters = [
            BrainProcessingJob.brain_id == brain.brain_id,
            BrainProcessingJob.owner_id == brain.owner_id,
        ]
        if status:
            filters.append(BrainProcessingJob.status == status)
        if job_type:
            filters.append(BrainProcessingJob.job_type == job_type)

        base = select(BrainProcessingJob).where(*filters)
        total = int(
            await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        )
        result = await self.session.execute(
            base.order_by(BrainProcessingJob.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), total

    @_private_job_operation
    async def job_counts(self, brain: ManagedBrain) -> dict[str, int]:
        """Return the number of jobs per status for one Brain."""
        result = await self.session.execute(
            select(BrainProcessingJob.status, func.count(BrainProcessingJob.id))
            .where(
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.owner_id == brain.owner_id,
            )
            .group_by(BrainProcessingJob.status)
        )
        return {str(status): int(count) for status, count in result.all()}


def job_metadata(job: BrainProcessingJob) -> dict[str, Any]:
    """Return the transport representation of one job (no private payload)."""
    return {
        "job_id": str(job.id),
        "brain_id": str(job.brain_id),
        "owner_id": str(job.owner_id),
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


__all__ = [
    "BrainJobQueue",
    "JOB_COMPLETED",
    "JOB_DEAD",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JobEnvelope",
    "JobQueueError",
    "JobQueueFullError",
    "RESERVED_PAYLOAD_KEYS",
    "job_metadata",
]
