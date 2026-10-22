"""The Brain processing engine.

Every processing operation runs inside a :class:`BrainJobContext` that is bound
to exactly one Brain. The context carries the Brain row, the Brain's vault,
the Brain's connected apps and the Brain's dedicated runtime. Handlers never
receive an owner id from the job payload - they read it from the Brain row, so
a job can never process another user's data.

The handlers reuse the deterministic analyser that already powers
``/v1/brain/analyze``. Where an LLM would normally be used (summarisation,
entity extraction) the engine asks the Brain's *own* runtime, and falls back
to the deterministic analyser when that runtime is unavailable - always
labelling which one produced the result.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import deterministic_analysis
from app.brain_manager.db_models import BrainProcessingJob
from app.brain_manager.jobs.queue import BrainJobQueue, JobEnvelope
from app.brain_manager.lifecycle.manager import BrainRuntimeManager
from app.brain_manager.vault.apps import BrainAppConnectionService
from app.brain_manager.vault.service import BrainVaultService
from app.storage_db import ManagedBrain

logger = logging.getLogger(__name__)

#: Job types this engine understands.
JOB_CLASSIFICATION = "classification"
JOB_ENTITY_EXTRACTION = "entity_extraction"
JOB_SUMMARIZATION = "summarization"
JOB_EMBEDDING_GENERATION = "embedding_generation"
JOB_RETRIEVAL = "retrieval"
JOB_CONTEXT_CONSTRUCTION = "context_construction"
JOB_RELATIONSHIP_DETECTION = "relationship_detection"
JOB_DATA_SORTING = "data_sorting"
JOB_MEMORY_EXTRACTION = "memory_extraction"
JOB_CONVERSATION_PROCESSING = "conversation_processing"

#: Job types that need an app to be authorized before they may run.
_APP_SCOPED_JOBS = {
    JOB_CLASSIFICATION: ("memory", "write"),
    JOB_MEMORY_EXTRACTION: ("memory", "write"),
    JOB_SUMMARIZATION: ("memory", "read"),
    JOB_ENTITY_EXTRACTION: ("memory", "read"),
    JOB_RETRIEVAL: ("brain", "read"),
    JOB_CONTEXT_CONSTRUCTION: ("brain", "read"),
    JOB_RELATIONSHIP_DETECTION: ("records", "write"),
    JOB_DATA_SORTING: ("records", "write"),
    JOB_EMBEDDING_GENERATION: ("files", "write"),
    JOB_CONVERSATION_PROCESSING: ("brain", "read"),
}


@dataclass
class BrainJobContext:
    """The trusted context every handler runs in."""

    session: AsyncSession
    brain: ManagedBrain
    vault: BrainVaultService
    apps: BrainAppConnectionService
    runtime_manager: BrainRuntimeManager

    @property
    def brain_id(self) -> uuid.UUID:
        return self.brain.brain_id

    @property
    def owner_id(self) -> uuid.UUID:
        return self.brain.owner_id


JobHandler = Callable[[BrainJobContext, JobEnvelope], Awaitable[dict[str, Any]]]


class BrainProcessingEngine:
    """Runs Brain processing jobs under the correct Brain context."""

    def __init__(self, runtime_manager: BrainRuntimeManager | None = None) -> None:
        self._runtime_manager = runtime_manager
        self._handlers: dict[str, JobHandler] = {}
        self._register_default_handlers()

    @property
    def runtime_manager(self) -> BrainRuntimeManager:
        if self._runtime_manager is None:
            self._runtime_manager = BrainRuntimeManager()
        return self._runtime_manager

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Register (or replace) the handler of one job type."""
        self._handlers[job_type] = handler

    def supports(self, job_type: str) -> bool:
        return job_type in self._handlers

    # -- execution ---------------------------------------------------------
    async def run_job(
        self, session: AsyncSession, brain: ManagedBrain, job: BrainProcessingJob
    ) -> dict[str, Any]:
        """Execute one job and return its result payload."""
        handler = self._handlers.get(job.job_type)
        if handler is None:
            return {
                "status": "unsupported",
                "job_type": job.job_type,
                "detail": "No handler is registered for this job type",
            }

        envelope = JobEnvelope(
            id=job.id,
            brain_id=job.brain_id,
            owner_id=job.owner_id,
            job_type=job.job_type,
            status=job.status,
            payload=dict(job.payload or {}),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            created_at=job.created_at,
        )
        context = BrainJobContext(
            session=session,
            brain=brain,
            vault=BrainVaultService(session),
            apps=BrainAppConnectionService(session),
            runtime_manager=self.runtime_manager,
        )

        source_app = envelope.payload.get("source_app")
        if source_app:
            scope, action = _APP_SCOPED_JOBS.get(job.job_type, ("brain", "read"))
            authorized = await context.apps.has_permission(
                brain, str(source_app), scope, action
            )
            if not authorized:
                return {
                    "status": "forbidden",
                    "job_type": job.job_type,
                    "detail": (
                        f"App '{source_app}' is not authorized for {scope}:{action} "
                        "on this Brain"
                    ),
                }

        try:
            result = await handler(context, envelope)
        except Exception as exc:
            logger.warning(
                "Brain job %s (%s) failed: %s",
                envelope.id,
                job.job_type,
                type(exc).__name__,
            )
            return {
                "status": "error",
                "job_type": job.job_type,
                "error_code": type(exc).__name__,
                "detail": "The job handler raised an error",
            }
        result.setdefault("job_type", job.job_type)
        result.setdefault("brain_id", str(brain.brain_id))
        return result

    async def drain(
        self,
        session: AsyncSession,
        brain: ManagedBrain,
        *,
        limit: int = 10,
        max_jobs_per_brain: int = 64,
    ) -> list[dict[str, Any]]:
        """Run up to ``limit`` queued jobs of one Brain."""
        queue = BrainJobQueue(session, max_jobs_per_brain=max_jobs_per_brain)
        results: list[dict[str, Any]] = []
        for _ in range(max(0, limit)):
            job = await queue.claim_next(brain)
            if job is None:
                break
            result = await self.run_job(session, brain, job)
            if result.get("status") == "error":
                await queue.fail(job, error_code=str(result.get("error_code", "error")))
            else:
                await queue.complete(job, result_payload=result)
            results.append(result)
        return results

    # -- default handlers --------------------------------------------------
    def _register_default_handlers(self) -> None:
        self.register(JOB_CLASSIFICATION, self._handle_classification)
        self.register(JOB_ENTITY_EXTRACTION, self._handle_entity_extraction)
        self.register(JOB_SUMMARIZATION, self._handle_summarization)
        self.register(JOB_EMBEDDING_GENERATION, self._handle_embedding_generation)
        self.register(JOB_RETRIEVAL, self._handle_retrieval)
        self.register(JOB_CONTEXT_CONSTRUCTION, self._handle_context_construction)
        self.register(JOB_RELATIONSHIP_DETECTION, self._handle_relationship_detection)
        self.register(JOB_DATA_SORTING, self._handle_data_sorting)
        self.register(JOB_MEMORY_EXTRACTION, self._handle_memory_extraction)
        self.register(JOB_CONVERSATION_PROCESSING, self._handle_conversation_processing)

    async def _analyse(
        self, context: BrainJobContext, content: str, source_app: str | None
    ) -> tuple[list[dict[str, Any]], str]:
        """Analyse content with the Brain's own runtime when possible."""
        runtime = context.runtime_manager.handle(context.brain_id)
        if runtime is not None:
            try:
                from app.analysis import build_analysis_prompt, parse_analysis_response

                system, user = build_analysis_prompt(content, source_app=source_app)
                text = runtime.generate(
                    system,
                    user,
                    max_new_tokens=512,
                    temperature=0.2,
                    top_p=0.8,
                )
                segments = parse_analysis_response(text)
                if segments:
                    return segments, "llm"
            except Exception as exc:
                logger.info(
                    "Brain %s runtime could not analyse the content: %s",
                    context.brain_id,
                    type(exc).__name__,
                )
        return (
            deterministic_analysis(content, source_app=source_app, max_segments=12),
            "deterministic",
        )

    async def _handle_classification(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        content = str(job.payload.get("content") or "")
        if not content.strip():
            return {"status": "skipped", "detail": "no content to classify"}
        segments, mode = await self._analyse(context, content, job.payload.get("source_app"))
        return {
            "status": "ok",
            "analysis_mode": mode,
            "segments": [
                {
                    "index": segment.get("index"),
                    "category": segment.get("category"),
                    "record_type": segment.get("record_type"),
                    "confidence": segment.get("confidence"),
                }
                for segment in segments
            ],
        }

    async def _handle_entity_extraction(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        content = str(job.payload.get("content") or "")
        if not content.strip():
            return {"status": "skipped", "detail": "no content to analyse"}
        segments, mode = await self._analyse(context, content, job.payload.get("source_app"))
        entities: list[str] = []
        topics: list[str] = []
        for segment in segments:
            for entity in segment.get("entities") or []:
                if entity not in entities:
                    entities.append(entity)
            for topic in segment.get("topics") or []:
                if topic not in topics:
                    topics.append(topic)
        return {
            "status": "ok",
            "analysis_mode": mode,
            "entities": entities[:50],
            "topics": topics[:50],
        }

    async def _handle_summarization(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        content = str(job.payload.get("content") or "")
        if not content.strip():
            return {"status": "skipped", "detail": "no content to summarize"}
        segments, mode = await self._analyse(context, content, job.payload.get("source_app"))
        summary = " ".join(
            str(segment.get("summary") or segment.get("text") or "") for segment in segments
        ).strip()
        return {
            "status": "ok",
            "analysis_mode": mode,
            "summary": summary[:1000],
            "segment_count": len(segments),
        }

    async def _handle_embedding_generation(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        record_id = job.payload.get("record_id")
        if record_id is None:
            return {"status": "skipped", "detail": "no record to index"}
        try:
            record_uuid = uuid.UUID(str(record_id))
        except ValueError:
            return {"status": "skipped", "detail": "record_id is not a UUID"}
        record = await context.vault.get_record(context.brain, record_uuid)
        chunks = await context.vault.index_record(context.brain, record)
        return {"status": "ok", "record_id": str(record.id), "chunk_count": chunks}

    async def _handle_retrieval(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        query = str(job.payload.get("query") or "")
        if not query.strip():
            return {"status": "skipped", "detail": "no query to run"}
        limit = int(job.payload.get("limit") or 10)
        hits = await context.vault.semantic_search(context.brain, query, limit=limit)
        return {"status": "ok", "query": query[:200], "results": hits}

    async def _handle_context_construction(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        query = str(job.payload.get("query") or "")
        limit = int(job.payload.get("limit") or 5)
        hits = await context.vault.semantic_search(context.brain, query, limit=limit)
        records, _ = await context.vault.list_records(context.brain, limit=limit)
        return {
            "status": "ok",
            "query": query[:200],
            "retrieved": hits,
            "recent_records": [
                {
                    "record_id": str(record.id),
                    "record_type": record.record_type,
                    "title": record.title,
                }
                for record in records
            ],
        }

    async def _handle_relationship_detection(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        record_id = job.payload.get("record_id")
        if record_id is None:
            return {"status": "skipped", "detail": "no record to link"}
        try:
            record_uuid = uuid.UUID(str(record_id))
        except ValueError:
            return {"status": "skipped", "detail": "record_id is not a UUID"}
        record = await context.vault.get_record(context.brain, record_uuid)
        related = await context.vault.list_relationships(context.brain, record_id=record.id)
        return {
            "status": "ok",
            "record_id": str(record.id),
            "relationship_count": len(related),
        }

    async def _handle_data_sorting(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        records, total = await context.vault.list_records(
            context.brain, limit=int(job.payload.get("limit") or 50)
        )
        buckets: dict[str, int] = {}
        for record in records:
            key = record.category or record.record_type or "unclassified"
            buckets[key] = buckets.get(key, 0) + 1
        return {
            "status": "ok",
            "total": total,
            "buckets": buckets,
            "record_ids": [str(record.id) for record in records],
        }

    async def _handle_memory_extraction(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        content = str(job.payload.get("content") or "")
        if not content.strip():
            return {"status": "skipped", "detail": "no content to extract from"}
        segments, mode = await self._analyse(context, content, job.payload.get("source_app"))
        memories = [
            {
                "content": str(segment.get("text") or "")[:1000],
                "kind": "fact",
                "category": segment.get("category"),
                "source_app": job.payload.get("source_app"),
            }
            for segment in segments
        ]
        return {
            "status": "ok",
            "analysis_mode": mode,
            "memories": memories,
            "memory_count": len(memories),
        }

    async def _handle_conversation_processing(
        self, context: BrainJobContext, job: JobEnvelope
    ) -> dict[str, Any]:
        content = str(job.payload.get("content") or "")
        runtime = context.runtime_manager.handle(context.brain_id)
        if runtime is None:
            return {
                "status": "skipped",
                "detail": "no dedicated runtime is available for this Brain",
            }
        try:
            reply = runtime.generate(
                "You are the Personal Brain of one AURA user. Answer using only the "
                "approved context. Be concise and safe.",
                content,
                max_new_tokens=256,
                temperature=0.2,
                top_p=0.8,
            )
        except Exception as exc:
            return {
                "status": "error",
                "error_code": type(exc).__name__,
                "detail": "The Brain runtime could not produce a reply",
            }
        return {"status": "ok", "reply": reply, "runtime_id": runtime.runtime_id}


__all__ = [
    "BrainJobContext",
    "BrainProcessingEngine",
    "JOB_CLASSIFICATION",
    "JOB_CONTEXT_CONSTRUCTION",
    "JOB_CONVERSATION_PROCESSING",
    "JOB_DATA_SORTING",
    "JOB_EMBEDDING_GENERATION",
    "JOB_ENTITY_EXTRACTION",
    "JOB_MEMORY_EXTRACTION",
    "JOB_RELATIONSHIP_DETECTION",
    "JOB_RETRIEVAL",
    "JOB_SUMMARIZATION",
]
