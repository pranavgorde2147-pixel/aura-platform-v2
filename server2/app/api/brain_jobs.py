"""Processing-job API for one Brain.

Jobs are queued against a Brain and executed in that Brain's context. The Brain
and owner ids of a job are taken from the registry, never from the request
payload, so a job can never be re-targeted at another user's Brain.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.config import get_brain_manager_config
from app.brain_manager.jobs.engine import BrainProcessingEngine
from app.brain_manager.jobs.queue import BrainJobQueue, JobQueueFullError, job_metadata
from app.brain_manager.registry.service import BrainRegistryService
from app.security.brain import require_owner_id
from app.security.storage import require_internal_token
from app.storage_db import ManagedBrain, get_db

router = APIRouter(
    prefix="/v1/brains",
    tags=["Brain Processing"],
    dependencies=[Depends(require_internal_token)],
)


class JobCreate(BaseModel):
    job_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=100, ge=1, le=1000)


class JobDrainRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=100)


async def _resolve_brain(
    session: AsyncSession, brain_id: uuid.UUID, owner_id: uuid.UUID
) -> ManagedBrain:
    brain = await BrainRegistryService.get_owned(
        session, brain_id=brain_id, owner_id=owner_id
    )
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    return brain


@router.post("/{brain_id}/jobs", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(
    brain_id: uuid.UUID,
    payload: JobCreate,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Queue one processing job for one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    config = get_brain_manager_config()
    queue = BrainJobQueue(session, max_jobs_per_brain=config.max_jobs_per_brain)
    try:
        job = await queue.enqueue(
            brain,
            job_type=payload.job_type,
            payload=payload.payload,
            idempotency_key=payload.idempotency_key,
            priority=payload.priority,
            max_attempts=config.job_max_attempts,
        )
    except JobQueueFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    return job_metadata(job)


@router.get("/{brain_id}/jobs")
async def list_jobs(
    brain_id: uuid.UUID,
    job_status: str | None = Query(default=None, alias="status", max_length=32),
    job_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List the processing jobs of one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    config = get_brain_manager_config()
    queue = BrainJobQueue(session, max_jobs_per_brain=config.max_jobs_per_brain)
    jobs, total = await queue.list_jobs(
        brain, status=job_status, job_type=job_type, limit=limit, offset=offset
    )
    return {
        "brain_id": str(brain.brain_id),
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": await queue.job_counts(brain),
        "items": [job_metadata(job) for job in jobs],
    }


@router.post("/{brain_id}/jobs/drain")
async def drain_jobs(
    brain_id: uuid.UUID,
    payload: JobDrainRequest,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run the queued jobs of one Brain inside that Brain's context."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    config = get_brain_manager_config()
    engine = BrainProcessingEngine()
    results = await engine.drain(
        session,
        brain,
        limit=payload.limit,
        max_jobs_per_brain=config.max_jobs_per_brain,
    )
    return {
        "brain_id": str(brain.brain_id),
        "executed": len(results),
        "results": results,
    }


__all__ = ["router"]
