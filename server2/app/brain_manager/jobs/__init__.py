"""Brain-scoped processing: the job queue and the processing engine."""

from __future__ import annotations

from app.brain_manager.jobs.engine import (
    JOB_CLASSIFICATION,
    JOB_CONTEXT_CONSTRUCTION,
    JOB_CONVERSATION_PROCESSING,
    JOB_DATA_SORTING,
    JOB_EMBEDDING_GENERATION,
    JOB_ENTITY_EXTRACTION,
    JOB_MEMORY_EXTRACTION,
    JOB_RELATIONSHIP_DETECTION,
    JOB_RETRIEVAL,
    JOB_SUMMARIZATION,
    BrainJobContext,
    BrainProcessingEngine,
)
from app.brain_manager.jobs.queue import (
    JOB_COMPLETED,
    JOB_DEAD,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    BrainJobQueue,
    JobEnvelope,
    JobQueueError,
    JobQueueFullError,
    job_metadata,
)

__all__ = [
    "BrainJobContext",
    "BrainJobQueue",
    "BrainProcessingEngine",
    "JOB_CLASSIFICATION",
    "JOB_COMPLETED",
    "JOB_CONTEXT_CONSTRUCTION",
    "JOB_CONVERSATION_PROCESSING",
    "JOB_DATA_SORTING",
    "JOB_DEAD",
    "JOB_EMBEDDING_GENERATION",
    "JOB_ENTITY_EXTRACTION",
    "JOB_FAILED",
    "JOB_MEMORY_EXTRACTION",
    "JOB_QUEUED",
    "JOB_RELATIONSHIP_DETECTION",
    "JOB_RETRIEVAL",
    "JOB_RUNNING",
    "JOB_SUMMARIZATION",
    "JobEnvelope",
    "JobQueueError",
    "JobQueueFullError",
    "job_metadata",
]
