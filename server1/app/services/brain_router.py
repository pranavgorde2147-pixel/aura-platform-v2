"""Orchestration of the universal Brain processing pipeline.

This service is the heart of the shared context layer:

1. **Accept** an input from an application, validating that the application is
   allowed to submit it.
2. **Understand** it through the Brain analysis service (Server 2).
3. **Plan** one operation per plausible destination, resolved from the
   application registry — never from the raw text — and authorized against
   the user's real connections and permissions.
4. **Deliver** the operations that the policy allows to be delivered
   automatically, and park the rest for explicit user review.
5. **Remember** only what the policy and the user allowed, and only as a
   bounded summary.

The service never trusts client-supplied identifiers for security decisions:
the source application, the destinations and every permission are resolved
server-side.
"""

from __future__ import annotations

import json
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_record import AppRecord, AppRecordLink
from app.models.brain_memory import BrainMemory, MemoryKind, MemorySource
from app.models.brain_processing import (
    BrainProcessingOperation,
    BrainProcessingRun,
    OperationStatus,
    ProcessingStatus,
)
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.app_record import AppRecordRepository
from app.repositories.brain_memory import BrainMemoryRepository
from app.repositories.brain_processing import (
    BrainProcessingOperationRepository,
    BrainProcessingRunRepository,
)
from app.schemas.processing import ProcessingRequest
from app.services.app_registry import AppConnectionService, get_catalog_app
from app.services.classification_policy import ClassificationPolicy, SensitiveContentDetector
from app.services.content_analysis import AnalysisResult, ContentAnalysisService
from app.services.destination_resolver import DestinationResolver
from app.services.storage_adapters import (
    RecordPayload,
    StorageAdapterError,
    StoredRecord,
    build_dedupe_key,
    build_record_store,
)

logger = logging.getLogger(__name__)

#: Maximum size of the extracted item payload kept for a later retry.
MAX_OPERATION_TEXT_CHARS = 20000


class ProcessingError(Exception):
    """A safe, user-facing processing failure."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class ProcessingOutcome:
    """Everything the API layer needs to describe one processing run."""

    run: BrainProcessingRun
    operations: list[BrainProcessingOperation] = field(default_factory=list)
    records: list[StoredRecord] = field(default_factory=list)
    source_record: AppRecord | None = None
    memories: list[BrainMemory] = field(default_factory=list)
    # (segment index, memory) pairs so the API can report which item produced
    # which memory entry.
    memory_items: list[tuple[int, BrainMemory]] = field(default_factory=list)
    analysis: AnalysisResult | None = None
    duplicate: bool = False
    errors: list[dict[str, str]] = field(default_factory=list)


class BrainRouter:
    """Runs the understand -> route -> deliver -> remember pipeline."""

    def __init__(
        self,
        session: AsyncSession,
        policy: ClassificationPolicy | None = None,
        connections: AppConnectionService | None = None,
        analysis: ContentAnalysisService | None = None,
    ) -> None:
        self.session = session
        self.policy = policy or ClassificationPolicy.from_settings()
        self.connections = connections or AppConnectionService(session)
        self.resolver = DestinationResolver(self.connections)
        self.analysis = analysis or ContentAnalysisService(self.policy)
        self.run_repo = BrainProcessingRunRepository(session)
        self.operation_repo = BrainProcessingOperationRepository(session)
        self.record_repo = AppRecordRepository(session)
        self.memory_repo = BrainMemoryRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.secret_detector = SensitiveContentDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(
        self,
        user: User,
        request: ProcessingRequest,
        *,
        idempotency_key: str | None = None,
    ) -> ProcessingOutcome:
        """Process one submitted input end to end."""
        key = (idempotency_key or request.idempotency_key or "").strip() or None
        if key is None and request.input_id:
            input_hash = hashlib.sha256(request.input_id.encode("utf-8")).hexdigest()
            key = f"{request.source_app or 'unknown'}:{input_hash}"
        if key is None and self.policy.idempotency_required:
            raise ProcessingError(
                "idempotency_key_required",
                "Provide an Idempotency-Key header or stable input_id.",
                status_code=422,
            )

        source_auth = await self.resolver.authorize_source(user, request.source_app)
        if not source_auth.allowed:
            raise ProcessingError(
                code=source_auth.reason,
                message=self._authorization_message(source_auth.reason, source_auth.required_permission),
                status_code=403 if source_auth.reason != "source_app_required" else 400,
            )

        if request.related_record_ids and not await self.connections.has_active_permission(
            user, request.source_app, scope="records", action="write"
        ):
            raise ProcessingError(
                code="permission_missing",
                message="The source application is missing the records:write permission.",
                status_code=403,
            )

        if key:
            existing = await self.run_repo.find_by_idempotency_key(
                user_id=user.id,
                idempotency_key=key,
            )
            if existing is not None:
                if existing.content_summary != request.content.strip()[
                    : self.policy.memory_summary_max_chars
                ]:
                    raise ProcessingError(
                        "idempotency_key_conflict",
                        "This idempotency key already identifies different content.",
                        status_code=409,
                    )
                return await self._outcome(existing, duplicate=True)

        run = await self._create_run(user, request, key)
        analysis = await self.analysis.analyze(
            user_id=str(user.id),
            content=request.content,
            source_app=request.source_app,
            language=request.language,
            context=request.context or {},
        )
        run.analysis_mode = analysis.analysis_mode
        run.confidence_kind = analysis.confidence_kind
        run.status = ProcessingStatus.CLASSIFIED

        operations = await self._plan_operations(user, request, run, analysis)
        await self.session.flush()

        outcome = ProcessingOutcome(run=run, operations=operations, analysis=analysis)
        if request.consent.retain_source:
            outcome.source_record = await self._store_source_record(
                user, request, run, analysis
            )
            if outcome.source_record is not None:
                outcome.records.append(
                    StoredRecord(
                        record_id=outcome.source_record.id,
                        storage_type=outcome.source_record.storage_type,
                        object_reference=outcome.source_record.object_reference,
                        created=outcome.source_record.source_run_id == run.id,
                    )
                )
                await self._link_related_records(user, request, outcome.source_record)
        await self._deliver_ready(user, request, run, operations, outcome)
        await self._write_memories(user, request, run, analysis, outcome)
        await self._finalize_status(run, operations)
        await self.session.commit()
        return outcome

    async def _store_source_record(
        self,
        user: User,
        request: ProcessingRequest,
        run: BrainProcessingRun,
        analysis: AnalysisResult,
    ) -> AppRecord | None:
        """Keep one full source record and attach only server-produced metadata."""
        app = get_catalog_app(request.source_app or "")
        if app is None or not app.routable or app.storage_adapter == "none":
            return None

        source_reference = request.content_ref or request.input_id
        record = None
        if source_reference:
            record = await self.record_repo.find_by_source_reference(
                user_id=user.id,
                source_app=app.slug,
                source_reference=source_reference,
            )
            if record is not None:
                existing_hash = (record.record_metadata or {}).get("source_content_sha256")
                if (
                    (record.content is not None and record.content != request.content)
                    or (existing_hash is not None and existing_hash != hashlib.sha256(
                        request.content.encode("utf-8")
                    ).hexdigest())
                    or (
                        record.content is None
                        and record.storage_type == "object"
                        and existing_hash is None
                    )
                ):
                    raise ProcessingError(
                        "source_reference_conflict",
                        "This source reference already identifies different or unverifiable content.",
                        status_code=409,
                    )

        categories = list(dict.fromkeys(segment.category for segment in analysis.segments))
        topics = list(
            dict.fromkeys(topic for segment in analysis.segments for topic in segment.topics)
        )
        metadata = {
            "source_content_sha256": hashlib.sha256(
                request.content.encode("utf-8")
            ).hexdigest(),
            "analysis_mode": analysis.analysis_mode,
            "confidence_kind": analysis.confidence_kind,
            "segments": [
                {
                    "category": segment.category,
                    "record_type": segment.record_type,
                    "summary": segment.summary[:500],
                    "confidence": segment.confidence,
                    "entities": segment.entities[:50],
                    "topics": segment.topics[:50],
                    "occurred_at": (
                        segment.occurred_at.isoformat() if segment.occurred_at else None
                    ),
                }
                for segment in analysis.segments
            ],
        }
        if record is not None:
            changed = (
                record.categories != categories
                or record.topics != topics
                or record.record_metadata != metadata
            )
            if changed:
                record.categories = categories
                record.topics = topics
                record.record_metadata = metadata
                record.version += 1
            return record

        dedupe_reference = source_reference or request.content
        source_hash = hashlib.sha256(dedupe_reference.encode("utf-8")).hexdigest()
        dedupe_key = f"{app.slug}:source:{source_hash}"
        store = build_record_store(self.session, app.storage_adapter, self.record_repo)
        try:
            stored = await store.write(
                RecordPayload(
                    user_id=user.id,
                    destination_app=app.slug,
                    record_type="source_record",
                    content=request.content,
                    dedupe_key=dedupe_key,
                    ai_identity_id=run.ai_identity_id,
                    source_run_id=run.id,
                    source_reference=source_reference or run.input_id,
                    occurred_at=request.occurred_at,
                    categories=categories,
                    topics=topics,
                    metadata=metadata,
                    retention_policy="default",
                )
            )
        except Exception as exc:
            logger.warning(
                "Canonical source record persistence failed for app %s: %s",
                app.slug,
                type(exc).__name__,
            )
            raise ProcessingError(
                "source_record_storage_failed",
                "The source record could not be durably stored.",
                status_code=503,
            ) from exc
        record = await self.record_repo.get_for_user(
            user_id=user.id, record_id=stored.record_id
        )
        if record is None:
            raise ProcessingError(
                "source_record_unavailable",
                "The source record could not be loaded after storage.",
                status_code=503,
            )
        return record

    async def _link_related_records(
        self, user: User, request: ProcessingRequest, source_record: AppRecord
    ) -> None:
        """Create idempotent owner-scoped links to caller-selected records."""
        for raw_related_id in request.related_record_ids:
            try:
                related_id = uuid.UUID(raw_related_id)
            except ValueError as exc:
                raise ProcessingError(
                    "invalid_related_record_id",
                    "A related record identifier is invalid.",
                    status_code=422,
                ) from exc
            if related_id == source_record.id:
                continue
            related = await self.record_repo.get_for_user(
                user_id=user.id, record_id=related_id
            )
            if related is None:
                raise ProcessingError(
                    "related_record_not_found",
                    "A related record was not found.",
                    status_code=404,
                )
            existing = await self.record_repo.find_record_link(
                user_id=user.id,
                record_id=source_record.id,
                related_record_id=related_id,
                relation_type="related",
            )
            if existing is None:
                self.session.add(
                    AppRecordLink(
                        user_id=user.id,
                        record_id=source_record.id,
                        related_record_id=related_id,
                        relation_type="related",
                    )
                )
        await self.session.flush()

    async def confirm(
        self,
        user: User,
        run_id: uuid.UUID,
        items: list,
    ) -> ProcessingOutcome:
        """Deliver the items the user confirmed after review."""
        run = await self.run_repo.get_for_user(user_id=user.id, run_id=run_id)
        if run is None:
            raise ProcessingError("not_found", "Processing run not found", status_code=404)
        if run.status == ProcessingStatus.CANCELLED:
            raise ProcessingError("run_cancelled", "Processing run was cancelled", status_code=409)

        operations = await self.operation_repo.list_for_run(run.id)
        selected = {item.index for item in items}
        if not selected:
            selected = {
                operation.segment_index
                for operation in operations
                if operation.status
                in (OperationStatus.AWAITING_REVIEW, OperationStatus.AWAITING_AUTHORIZATION)
            }

        outcome = ProcessingOutcome(
            run=run,
            operations=operations,
            source_record=await self._source_record_for_run(run),
        )
        request = ProcessingRequest(
            content=run.content_summary or " ",
            source_app=run.source_app,
            input_id=run.input_id,
        )

        for operation in operations:
            if operation.segment_index not in selected:
                continue
            if operation.status == OperationStatus.DELIVERED:
                continue
            override = next((item for item in items if item.index == operation.segment_index), None)
            if override is not None:
                if override.destination_app:
                    operation.destination_app = override.destination_app
                if override.record_type:
                    operation.record_type = override.record_type
            auth = await self.resolver.authorize_destination(
                user,
                operation.destination_app,
                operation.record_type,
            )
            if not auth.allowed:
                operation.status = OperationStatus.AWAITING_AUTHORIZATION
                operation.error_code = auth.reason
                operation.requires_review = True
                outcome.errors.append(
                    {
                        "code": auth.reason,
                        "message": self._authorization_message(
                            auth.reason, auth.required_permission
                        ),
                        "stage": "authorization",
                    }
                )
                continue
            operation.status = OperationStatus.READY
            operation.requires_review = False
            operation.requires_confirmation = False

        await self.session.flush()
        await self._deliver_ready(user, request, run, operations, outcome)
        await self._finalize_status(run, operations)
        await self.session.commit()
        return outcome

    async def retry(self, user: User, run_id: uuid.UUID) -> ProcessingOutcome:
        """Retry the operations of a run that did not succeed."""
        run = await self.run_repo.get_for_user(user_id=user.id, run_id=run_id)
        if run is None:
            raise ProcessingError("not_found", "Processing run not found", status_code=404)
        if run.status == ProcessingStatus.CANCELLED:
            raise ProcessingError("run_cancelled", "Processing run was cancelled", status_code=409)

        operations = await self.operation_repo.list_for_run(run.id)
        outcome = ProcessingOutcome(
            run=run,
            operations=operations,
            source_record=await self._source_record_for_run(run),
        )
        request = ProcessingRequest(
            content=run.content_summary or " ",
            source_app=run.source_app,
            input_id=run.input_id,
        )

        for operation in operations:
            if operation.status in (OperationStatus.DELIVERED, OperationStatus.SKIPPED):
                continue
            if operation.attempts >= self.policy.max_delivery_retries:
                continue
            auth = await self.resolver.authorize_destination(
                user,
                operation.destination_app,
                operation.record_type,
            )
            if not auth.allowed:
                operation.status = OperationStatus.AWAITING_AUTHORIZATION
                operation.error_code = auth.reason
                continue
            operation.status = OperationStatus.READY
            operation.error_code = None

        run.retry_count += 1
        run.status = ProcessingStatus.RETRY_PENDING
        await self.session.flush()
        await self._deliver_ready(user, request, run, operations, outcome)
        await self._finalize_status(run, operations)
        await self.session.commit()
        return outcome

    async def cancel(self, user: User, run_id: uuid.UUID) -> ProcessingOutcome:
        """Cancel a run that has not been delivered yet."""
        run = await self.run_repo.get_for_user(user_id=user.id, run_id=run_id)
        if run is None:
            raise ProcessingError("not_found", "Processing run not found", status_code=404)
        if run.status == ProcessingStatus.DELIVERED:
            raise ProcessingError(
                "already_delivered",
                "This run was already delivered and cannot be cancelled",
                status_code=409,
            )

        operations = await self.operation_repo.list_for_run(run.id)
        for operation in operations:
            if operation.status == OperationStatus.DELIVERED:
                continue
            operation.status = OperationStatus.CANCELLED
        run.status = ProcessingStatus.CANCELLED
        await self.session.flush()
        await self.session.commit()
        return ProcessingOutcome(run=run, operations=operations)

    async def get_run(self, user: User, run_id: uuid.UUID) -> ProcessingOutcome:
        """Return a run owned by the user together with its operations."""
        run = await self.run_repo.get_for_user(user_id=user.id, run_id=run_id)
        if run is None:
            raise ProcessingError("not_found", "Processing run not found", status_code=404)
        return await self._outcome(run)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _create_run(
        self,
        user: User,
        request: ProcessingRequest,
        idempotency_key: str | None,
    ) -> BrainProcessingRun:
        summary = request.content.strip()[: self.policy.memory_summary_max_chars]
        run = BrainProcessingRun(
            user_id=user.id,
            source_app=request.source_app or "unknown",
            input_id=request.input_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            input_type=request.input_type,
            status=ProcessingStatus.RECEIVED,
            language=request.language,
            session_id=request.session_id,
            source_reference=request.content_ref,
            content_summary=summary,
            occurred_at=request.occurred_at,
            captured_at=request.captured_at or datetime.now(timezone.utc),
            review_required=False,
            retry_count=0,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def _plan_operations(
        self,
        user: User,
        request: ProcessingRequest,
        run: BrainProcessingRun,
        analysis: AnalysisResult,
    ) -> list[BrainProcessingOperation]:
        """Create one operation per plausible destination for every item."""
        operations: list[BrainProcessingOperation] = []
        requested = list(request.requested_destinations or [])
        if not request.consent.cross_app_routing and request.source_app:
            requested = [request.source_app]

        for segment in analysis.segments:
            candidates = self.resolver.candidates_for(
                segment.category,
                segment.record_type,
                requested,
            )
            if not candidates:
                operation = BrainProcessingOperation(
                    run_id=run.id,
                    user_id=user.id,
                    destination_app=request.source_app or "unknown",
                    record_type=segment.record_type,
                    segment_index=segment.index,
                    category=segment.category,
                    status=OperationStatus.SKIPPED,
                    confidence=segment.confidence,
                    attempts=0,
                    error_code="no_destination",
                    requires_review=True,
                    operation_metadata=self._segment_metadata(segment),
                )
                self.session.add(operation)
                operations.append(operation)
                continue

            for candidate in candidates:
                app = candidate.app
                auth = await self.resolver.authorize_destination(
                    user,
                    app.slug,
                    candidate.record_type,
                )
                if not auth.allowed:
                    status = OperationStatus.AWAITING_AUTHORIZATION
                elif self.policy.accepts(segment.category, segment.confidence):
                    status = OperationStatus.READY
                else:
                    status = OperationStatus.AWAITING_REVIEW

                requires_confirmation = (
                    auth.allowed and self.policy.requires_confirmation(segment.category)
                )
                operation = BrainProcessingOperation(
                    run_id=run.id,
                    user_id=user.id,
                    destination_app=app.slug,
                    record_type=candidate.record_type,
                    segment_index=segment.index,
                    category=segment.category,
                    status=status,
                    confidence=segment.confidence,
                    attempts=0,
                    error_code=None if auth.allowed else auth.reason,
                    requires_review=status
                    in (OperationStatus.AWAITING_REVIEW, OperationStatus.AWAITING_AUTHORIZATION),
                    requires_confirmation=requires_confirmation,
                    operation_metadata=self._segment_metadata(
                        segment,
                        candidates=[candidate.app.slug for candidate in candidates],
                        required_scope=auth.required_scope,
                        required_action=auth.required_action,
                    ),
                )
                self.session.add(operation)
                operations.append(operation)
        return operations

    def _segment_metadata(
        self,
        segment,
        candidates: list[str] | None = None,
        required_scope: str | None = None,
        required_action: str | None = None,
    ) -> str:
        """Serialise the extracted item so it can be re-delivered later."""
        payload = {
            "summary": segment.summary,
            "rationale": segment.rationale,
            "entities": segment.entities,
            "topics": segment.topics,
            "occurred_at": segment.occurred_at.isoformat() if segment.occurred_at else None,
            "text": segment.text[:MAX_OPERATION_TEXT_CHARS],
        }
        if candidates:
            payload["candidates"] = candidates
        if required_scope:
            payload["required_scope"] = required_scope
        if required_action:
            payload["required_action"] = required_action
        return json.dumps(payload)

    @staticmethod
    def _segment_text(operation: BrainProcessingOperation) -> str:
        """Recover the extracted item text stored with an operation."""
        if not operation.operation_metadata:
            return ""
        try:
            payload = json.loads(operation.operation_metadata)
        except (TypeError, ValueError):
            return ""
        text = payload.get("text")
        return text if isinstance(text, str) else ""

    async def _deliver_ready(
        self,
        user: User,
        request: ProcessingRequest,
        run: BrainProcessingRun,
        operations: list[BrainProcessingOperation],
        outcome: ProcessingOutcome,
    ) -> None:
        """Write the records for every operation that may be delivered."""
        for operation in operations:
            if operation.status != OperationStatus.READY:
                continue
            app = get_catalog_app(operation.destination_app)
            if app is None:
                operation.status = OperationStatus.FAILED
                operation.error_code = "unknown_app"
                continue

            text = self._segment_text(operation)
            if not text:
                operation.status = OperationStatus.FAILED
                operation.error_code = "content_unavailable"
                continue

            if outcome.source_record is not None:
                if operation.destination_app != outcome.source_record.destination_app:
                    link = await self.record_repo.find_app_link(
                        user_id=user.id,
                        record_id=outcome.source_record.id,
                        target_app=operation.destination_app,
                        relation_type="classified_for",
                    )
                    if link is None:
                        self.session.add(
                            AppRecordLink(
                                user_id=user.id,
                                record_id=outcome.source_record.id,
                                target_app=operation.destination_app,
                                relation_type="classified_for",
                            )
                        )
                operation.status = OperationStatus.DELIVERED
                operation.error_code = None
                operation.record_reference = str(outcome.source_record.id)
                continue

            store = build_record_store(self.session, app.storage_adapter, self.record_repo)
            segment_metadata = self._operation_metadata(operation)
            payload = RecordPayload(
                user_id=user.id,
                destination_app=operation.destination_app,
                record_type=operation.record_type,
                content=text,
                dedupe_key=build_dedupe_key(
                    user_id=user.id,
                    destination_app=operation.destination_app,
                    record_type=operation.record_type,
                    run_id=run.id,
                    segment_index=operation.segment_index,
                    content=text,
                ),
                title=None,
                summary=self._segment_summary(operation),
                source_run_id=run.id,
                source_segment_index=operation.segment_index,
                source_reference=run.source_reference,
                occurred_at=self._segment_occurred_at(operation) or run.occurred_at,
                categories=[operation.category] if operation.category else [],
                topics=list(segment_metadata.get("topics") or []),
                metadata={
                    "analysis_mode": run.analysis_mode,
                    "confidence_kind": run.confidence_kind,
                    "confidence": operation.confidence,
                    "entities": list(segment_metadata.get("entities") or []),
                    "rationale": segment_metadata.get("rationale"),
                },
                retention_policy="default",
            )

            operation.status = OperationStatus.DELIVERING
            operation.attempts += 1
            try:
                stored = await store.write(payload)
            except StorageAdapterError as exc:
                operation.status = OperationStatus.FAILED
                operation.error_code = exc.code
                outcome.errors.append(
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "stage": "delivery",
                    }
                )
                continue
            except Exception:  # pragma: no cover - defensive
                logger.exception("Unexpected delivery failure for %s", operation.destination_app)
                operation.status = OperationStatus.FAILED
                operation.error_code = "delivery_failed"
                outcome.errors.append(
                    {
                        "code": "delivery_failed",
                        "message": "The record could not be stored",
                        "stage": "delivery",
                    }
                )
                continue

            operation.status = OperationStatus.DELIVERED
            operation.error_code = None
            operation.record_reference = stored.object_reference or str(stored.record_id)
            outcome.records.append(stored)

    @staticmethod
    def _operation_metadata(operation: BrainProcessingOperation) -> dict:
        if not operation.operation_metadata:
            return {}
        try:
            payload = json.loads(operation.operation_metadata)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _segment_summary(operation: BrainProcessingOperation) -> str | None:
        payload = BrainRouter._operation_metadata(operation)
        summary = payload.get("summary")
        return summary[:500] if isinstance(summary, str) else None

    @staticmethod
    def _segment_occurred_at(operation: BrainProcessingOperation) -> datetime | None:
        payload = BrainRouter._operation_metadata(operation)
        raw = payload.get("occurred_at")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def _write_memories(
        self,
        user: User,
        request: ProcessingRequest,
        run: BrainProcessingRun,
        analysis: AnalysisResult,
        outcome: ProcessingOutcome,
    ) -> None:
        """Store the bounded memory entries the policy allows."""
        if not request.persist_memory or not request.consent.memory_write:
            return
        if not self.policy.memory_write_enabled:
            return

        identity = await self.identity_repo.get_by_user_id(user.id)
        if identity is None:
            outcome.errors.append(
                {
                    "code": "no_ai_identity",
                    "message": "No AI identity is available for memory writes",
                    "stage": "memory",
                }
            )
            return

        for segment in analysis.segments:
            if not self.policy.memory_allowed(segment.category):
                continue
            if self.secret_detector.contains_secret(segment.text):
                outcome.errors.append(
                    {
                        "code": "sensitive_content_skipped",
                        "message": "An item containing credentials was not stored in memory",
                        "stage": "memory",
                    }
                )
                continue

            content = segment.summary[: self.policy.memory_summary_max_chars]
            kind = (
                MemoryKind.FACT
                if segment.category in ("personal_fact", "user_preference")
                else MemoryKind.INTERPRETATION
            )
            expires_at = None
            if self.policy.memory_retention_days > 0:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    days=self.policy.memory_retention_days
                )
            memory = await self.memory_repo.create(
                ai_identity_id=identity.id,
                kind=kind,
                source=MemorySource.APP,
                content=content,
                summary=content[:512],
                source_app=run.source_app,
                importance=1,
                retention_policy="default",
                expires_at=expires_at,
            )
            outcome.memories.append(memory)
            outcome.memory_items.append((segment.index, memory))

    async def _finalize_status(
        self,
        run: BrainProcessingRun,
        operations: list[BrainProcessingOperation],
    ) -> None:
        """Derive the run status from the state of its operations."""
        if not operations:
            run.status = ProcessingStatus.AWAITING_REVIEW
            run.review_required = True
            return

        delivered = [op for op in operations if op.status == OperationStatus.DELIVERED]
        pending_review = [
            op
            for op in operations
            if op.status
            in (OperationStatus.AWAITING_REVIEW, OperationStatus.AWAITING_AUTHORIZATION)
        ]
        failed = [op for op in operations if op.status == OperationStatus.FAILED]
        skipped = [op for op in operations if op.status == OperationStatus.SKIPPED]

        if pending_review:
            run.status = ProcessingStatus.AWAITING_REVIEW
            run.review_required = True
        elif failed and delivered:
            run.status = ProcessingStatus.PARTIALLY_DELIVERED
            run.review_required = False
        elif failed and not delivered:
            run.status = ProcessingStatus.FAILED
            run.review_required = False
        elif delivered and skipped and len(delivered) == len(operations) - len(skipped):
            run.status = ProcessingStatus.DELIVERED
            run.review_required = False
        elif delivered:
            run.status = ProcessingStatus.DELIVERED
            run.review_required = False
        else:
            run.status = ProcessingStatus.AWAITING_REVIEW
            run.review_required = True

    async def _outcome(self, run: BrainProcessingRun, duplicate: bool = False) -> ProcessingOutcome:
        operations = await self.operation_repo.list_for_run(run.id)
        source_record = await self._source_record_for_run(run)
        if source_record is None:
            for operation in operations:
                if not operation.record_reference:
                    continue
                try:
                    record_id = uuid.UUID(operation.record_reference)
                except ValueError:
                    continue
                source_record = await self.record_repo.get_for_user(
                    user_id=run.user_id, record_id=record_id
                )
                if source_record is not None:
                    break
        records = [
            StoredRecord(
                record_id=record.id,
                storage_type=record.storage_type,
                object_reference=record.object_reference,
                created=False,
            )
            for record in await self.record_repo.list_by_run(run.id)
        ]
        return ProcessingOutcome(
            run=run,
            operations=operations,
            records=records,
            source_record=source_record,
            duplicate=duplicate,
        )

    async def _source_record_for_run(
        self, run: BrainProcessingRun
    ) -> AppRecord | None:
        reference = run.source_reference or run.input_id
        return await self.record_repo.find_by_source_reference(
            user_id=run.user_id,
            source_app=run.source_app,
            source_reference=reference,
        )

    @staticmethod
    def _authorization_message(reason: str, required_permission: str | None) -> str:
        """Map an authorization reason to a safe, actionable message."""
        messages = {
            "unknown_app": "The application is not registered.",
            "source_app_required": "A source application is required.",
            "source_app_not_enabled_for_ingest": "This application cannot submit content.",
            "connection_missing": "Connect the application before using it.",
            "connection_inactive": "The application connection is inactive.",
            "permission_missing": "The application is missing a required permission.",
            "not_routable": "The application does not accept routed records.",
            "no_storage_integration": "The application has no storage integration.",
            "unsupported_record_type": "The application does not support this record type.",
            "no_write_permission_declared": "The application does not declare a write permission.",
        }
        message = messages.get(reason, "The request is not authorized.")
        if required_permission and reason == "permission_missing":
            return f"{message} Required permission: {required_permission}."
        return message
