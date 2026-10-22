"""API boundary for the universal Brain processing pipeline.

Every application talks to this endpoint. The API layer is deliberately thin:
it authenticates the caller, hands the request to the routing service and
translates the outcome into the structured response contract.

Security properties enforced here:

* the user identity always comes from the authenticated token, never from the
  request body;
* the source application and the requested destinations are untrusted hints
  that the service validates against the registry and the permission system;
* errors are reported with stable codes and safe messages.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.models.brain_processing import BrainProcessingOperation, OperationStatus
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.schemas.processing import (
    AuthorizationOutcome,
    ClassificationSummary,
    ConfirmRequest,
    DeliveryOutcome,
    DestinationDecision,
    MemoryOperation,
    ProcessedItem,
    ProcessingError,
    ProcessingRequest,
    ProcessingResponse,
    RecordReference,
)
from app.services.app_registry import AppConnectionService, get_catalog_app
from app.services.classification_policy import ClassificationPolicy

router = APIRouter(prefix="/brain/process", tags=["Brain"])


@router.post("", response_model=ProcessingResponse, status_code=status.HTTP_201_CREATED)
async def process_input(
    payload: ProcessingRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Submit one input for understanding, routing and delivery.

    The response contains the extracted items, the destinations that were
    considered, the authorization decision for each of them and the delivery
    outcome. Items that were not confident enough to be delivered automatically
    are returned with ``requires_review`` so the client can ask the user.
    """
    source_app = payload.source_app or "brain"
    app = get_catalog_app(source_app)
    if app is None:
        raise HTTPException(status_code=404, detail="Unknown source application")
    if payload.source_app and not await AppConnectionService(session).has_active_permission(
        current_user, source_app, "memory", "write"
    ):
        raise HTTPException(status_code=403, detail="Source app is not authorized to write")
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(status_code=503, detail="Personal Brain is not ready")

    client = get_brain_client()
    operation_key = idempotency_key or payload.idempotency_key or str(uuid.uuid4())
    key_hash = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
    processing_id = uuid.uuid5(current_user.id, key_hash)
    request_id = str(uuid.uuid4())
    requested_destinations = sorted(set(payload.requested_destinations))
    unknown_destinations = [
        destination
        for destination in requested_destinations
        if get_catalog_app(destination) is None
    ]
    if unknown_destinations:
        raise HTTPException(status_code=422, detail="Unknown requested destination")
    blocked_destinations = [
        destination
        for destination in requested_destinations
        if destination != source_app
    ]
    try:
        analysis = await client.analyze(
            user_id=str(current_user.id),
            content=payload.content,
            context=payload.context,
            source_app=source_app,
            language=payload.language,
            request_id=request_id,
        )
        segments = analysis.get("segments")
        if not isinstance(segments, list) or not segments:
            raise BrainServiceError(
                "brain_invalid_response", "AURA Brain returned no classified segments"
            )
        items: list[ProcessedItem] = []
        records: list[RecordReference] = []
        deliveries: list[DeliveryOutcome] = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            content = str(segment.get("text") or payload.content)
            record = await client.vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain.id),
                method="POST",
                path="records",
                json_body={
                    "record_type": str(segment.get("record_type") or payload.input_type)[:64],
                    "namespace": f"apps/{source_app}",
                    "idempotency_key": f"process:{key_hash}:{index}",
                    "title": str(segment.get("summary") or content[:120])[:512],
                    "content": content,
                    "source_app": source_app,
                    "source_reference": str(processing_id),
                    "metadata": {
                        "processing_id": str(processing_id),
                        "segment_index": index,
                        "input_id": payload.input_id,
                        "input_type": payload.input_type,
                        "category": segment.get("category"),
                        "summary": segment.get("summary"),
                        "topics": segment.get("topics") or [],
                        "entities": segment.get("entities") or [],
                        "occurred_at": segment.get("occurred_at"),
                        "confidence": segment.get("confidence"),
                        "confidence_kind": analysis.get("confidence_kind", "heuristic"),
                        "analysis_mode": analysis.get("analysis_mode", "deterministic"),
                        "blocked_destinations": blocked_destinations,
                        "is_original": True,
                    },
                },
            )
            record_id = str(record["record_id"])
            items.append(
                ProcessedItem(
                    index=len(items),
                    category=str(segment.get("category") or "unclassified"),
                    record_type=str(segment.get("record_type") or payload.input_type),
                    summary=str(segment.get("summary") or content[:200])[:500],
                    confidence=max(0.0, min(1.0, float(segment.get("confidence") or 0))),
                    confidence_kind=str(analysis.get("confidence_kind") or "heuristic"),
                    entities=list(segment.get("entities") or []),
                    topics=list(segment.get("topics") or []),
                    occurred_at=segment.get("occurred_at"),
                    source_reference=str(processing_id),
                    destination_app=source_app,
                    status="delivered",
                )
            )
            records.append(
                RecordReference(
                    destination_app=source_app,
                    record_type=str(segment.get("record_type") or payload.input_type),
                    record_id=record_id,
                    storage_type="brain_vault",
                )
            )
            deliveries.append(
                DeliveryOutcome(
                    destination_app=source_app,
                    record_type=str(segment.get("record_type") or payload.input_type),
                    status="delivered",
                    record_reference=record_id,
                    attempts=1,
                )
            )
    except BrainServiceError as exc:
        status_code = 409 if exc.status_code == 409 else 503
        raise HTTPException(
            status_code=status_code,
            detail="Personal Brain processing or storage is unavailable.",
        ) from exc
    if not items:
        raise HTTPException(status_code=503, detail="Personal Brain returned no usable items")
    authorization = [
        AuthorizationOutcome(
            destination_app=destination,
            record_type=items[0].record_type,
            allowed=False,
            reason="cross_app_delivery_is_not_enabled_for_brain_vault",
        )
        for destination in requested_destinations
        if destination != source_app
    ]
    return ProcessingResponse(
        input_id=payload.input_id or operation_key,
        processing_id=processing_id,
        source_app=source_app,
        status="awaiting_review" if authorization else "delivered",
        items=items,
        classification=ClassificationSummary(
            analysis_mode=str(analysis.get("analysis_mode") or "deterministic"),
            confidence_kind=str(analysis.get("confidence_kind") or "heuristic"),
            segment_count=len(items),
            categories=sorted({item.category for item in items}),
        ),
        authorization=authorization,
        memory_operations=[
            MemoryOperation(
                action="stored" if payload.persist_memory or payload.consent.memory_write else "skipped",
                item_index=item.index,
                memory_id=records[item.index].record_id,
                kind="fact",
                content_preview=item.summary[:120],
            )
            for item in items
        ],
        delivery=deliveries,
        record_references=records,
        review_required=bool(authorization),
        idempotency_key=operation_key,
    )


@router.get("/{processing_id}", response_model=ProcessingResponse)
async def get_processing_run(
    processing_id: uuid.UUID,
    client_app: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Rebuild processing status from the owner's Brain vault records."""
    if get_catalog_app(client_app) is None or not await AppConnectionService(
        session
    ).has_active_permission(current_user, client_app, "brain", "read"):
        raise HTTPException(status_code=403, detail="App is not authorized to read processing status")
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(status_code=503, detail="Personal Brain is not ready")
    connections = AppConnectionService(session)
    if client_app == "brain":
        namespaces = {"shared", "documents", "apps/brain"}
        namespaces.update(
            f"apps/{module.module_slug}"
            for module in await connections.list_connections(current_user)
            if module.is_active
            and connections.has_permission(module, "brain", "read")
        )
    else:
        namespaces = {f"apps/{client_app}"}
    client = get_brain_client()
    try:
        records = []
        for namespace in sorted(namespaces):
            listing = await client.vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain.id),
                method="GET",
                path="records",
                params={
                    "namespace": namespace,
                    "source_reference": str(processing_id),
                    "limit": 50,
                },
            )
            records.extend(listing.get("items", []))
    except BrainServiceError as exc:
        raise HTTPException(
            status_code=503, detail="Processing records are unavailable"
        ) from exc
    if not records:
        raise HTTPException(status_code=404, detail="Processing run not found")
    records.sort(
        key=lambda record: (record.get("metadata") or {}).get("segment_index", 0)
    )
    metadata_items = [record.get("metadata") or {} for record in records]
    source_app = str(records[0].get("source_app") or "brain")
    if source_app != client_app and client_app != "brain":
        raise HTTPException(status_code=404, detail="Processing run not found")
    blocked_destinations = metadata_items[0].get("blocked_destinations", [])
    record_type = str(records[0].get("record_type") or "note")
    authorization = [
        AuthorizationOutcome(
            destination_app=destination,
            record_type=record_type,
            allowed=False,
            reason="cross_app_delivery_is_not_enabled_for_brain_vault_gateway",
        )
        for destination in blocked_destinations
    ]
    item_status = "awaiting_review" if authorization else "delivered"
    items = [
        ProcessedItem(
            index=index,
            category=str(metadata.get("category") or "unclassified"),
            record_type=str(record.get("record_type") or "note"),
            summary=str(metadata.get("summary") or record.get("title") or "")[:500],
            confidence=max(0.0, min(1.0, float(metadata.get("confidence") or 0))),
            confidence_kind=str(metadata.get("confidence_kind") or "heuristic"),
            entities=list(metadata.get("entities") or []),
            topics=list(metadata.get("topics") or []),
            occurred_at=metadata.get("occurred_at"),
            source_reference=str(processing_id),
            destination_app=source_app,
            status=item_status,
        )
        for index, (record, metadata) in enumerate(zip(records, metadata_items))
    ]
    return ProcessingResponse(
        input_id=str(metadata_items[0].get("input_id") or processing_id),
        processing_id=processing_id,
        source_app=source_app,
        status=item_status,
        items=items,
        classification=ClassificationSummary(
            analysis_mode=str(metadata_items[0].get("analysis_mode") or "deterministic"),
            confidence_kind=str(metadata_items[0].get("confidence_kind") or "heuristic"),
            segment_count=len(items),
            categories=sorted({item.category for item in items}),
        ),
        authorization=authorization,
        review_required=bool(authorization),
        delivery=[
            DeliveryOutcome(
                destination_app=source_app,
                record_type=item.record_type,
                status="delivered",
                record_reference=record["record_id"],
            )
            for item, record in zip(items, records)
        ],
        record_references=[
            RecordReference(
                destination_app=source_app,
                record_type=record["record_type"],
                record_id=record["record_id"],
                storage_type="brain_vault",
            )
            for record in records
        ],
    )


@router.post("/{processing_id}/confirm", response_model=ProcessingResponse)
async def confirm_processing_run(
    processing_id: uuid.UUID,
    payload: ConfirmRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Deliver the items the user confirmed after a review."""
    raise HTTPException(
        status_code=410,
        detail="Cross-app delivery is not available through the Brain vault gateway.",
    )


@router.post("/{processing_id}/retry", response_model=ProcessingResponse)
async def retry_processing_run(
    processing_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Retry the operations of a run that did not succeed."""
    raise HTTPException(
        status_code=410,
        detail="Submit the input again using the same Idempotency-Key.",
    )


@router.post("/{processing_id}/cancel", response_model=ProcessingResponse)
async def cancel_processing_run(
    processing_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Cancel a run that has not been delivered yet."""
    raise HTTPException(
        status_code=410,
        detail="Legacy processing runs are not available from Render.",
    )


# --------------------------------------------------------------------------
# Response assembly
# --------------------------------------------------------------------------


def _build_response(outcome) -> ProcessingResponse:
    """Translate a routing outcome into the public response contract."""
    run = outcome.run
    operations = outcome.operations
    policy = ClassificationPolicy.from_settings()

    grouped: dict[int, list[BrainProcessingOperation]] = {}
    for operation in operations:
        grouped.setdefault(operation.segment_index, []).append(operation)

    items: list[ProcessedItem] = []
    destinations: list[DestinationDecision] = []
    authorization: list[AuthorizationOutcome] = []
    delivery: list[DeliveryOutcome] = []

    for index in sorted(grouped):
        group = grouped[index]
        primary = group[0]
        metadata = _parse_metadata(primary)
        item_status = _item_status(group)
        items.append(
            ProcessedItem(
                index=index,
                category=primary.category or "unclassified",
                record_type=primary.record_type,
                summary=metadata.get("summary") or "",
                confidence=primary.confidence,
                confidence_kind=run.confidence_kind or "heuristic",
                entities=list(metadata.get("entities") or []),
                topics=list(metadata.get("topics") or []),
                occurred_at=_parse_datetime(metadata.get("occurred_at")),
                source_reference=run.source_reference or run.input_id,
                destination_app=_primary_destination(group),
                status=item_status,
                requires_review=any(op.requires_review for op in group),
                rationale=metadata.get("rationale") or None,
            )
        )
        for operation in group:
            operation_metadata = _parse_metadata(operation)
            destinations.append(
                DestinationDecision(
                    destination_app=operation.destination_app,
                    record_type=operation.record_type,
                    category=operation.category or "unclassified",
                    confidence=operation.confidence,
                    status=operation.status.value,
                    candidates=list(operation_metadata.get("candidates") or []),
                )
            )
            authorization.append(
                AuthorizationOutcome(
                    destination_app=operation.destination_app,
                    record_type=operation.record_type,
                    allowed=operation.status
                    in (
                        OperationStatus.DELIVERED,
                        OperationStatus.READY,
                        OperationStatus.AWAITING_REVIEW,
                    ),
                    reason=operation.error_code,
                    required_scope=operation_metadata.get("required_scope"),
                    required_action=operation_metadata.get("required_action"),
                    required_permission=_required_permission(operation_metadata),
                )
            )
            delivery.append(
                DeliveryOutcome(
                    destination_app=operation.destination_app,
                    record_type=operation.record_type,
                    status=_delivery_status(operation.status),
                    record_reference=operation.record_reference,
                    error_code=operation.error_code,
                    attempts=operation.attempts,
                )
            )

    memory_operations = [
        MemoryOperation(
            action="stored",
            item_index=item_index,
            memory_id=str(memory.id),
            kind=memory.kind.value,
            reason=None,
            content_preview=memory.content[:200],
        )
        for item_index, memory in outcome.memory_items
    ]

    categories = sorted({item.category for item in items})
    thresholds = {category: policy.threshold_for(category) for category in categories}
    analysis = outcome.analysis
    classification = ClassificationSummary(
        analysis_mode=run.analysis_mode or (analysis.analysis_mode if analysis else "deterministic"),
        confidence_kind=run.confidence_kind
        or (analysis.confidence_kind if analysis else "heuristic"),
        segment_count=len(items),
        categories=categories,
        thresholds=thresholds,
    )

    record_references = [
        RecordReference(
            destination_app=operation.destination_app,
            record_type=operation.record_type,
            record_id=operation.record_reference or "",
            storage_type=(
                "object" if operation.record_reference and _looks_like_object_id(operation) else "relational"
            ),
        )
        for operation in operations
        if operation.status == OperationStatus.DELIVERED and operation.record_reference
    ]

    return ProcessingResponse(
        input_id=run.input_id,
        processing_id=run.id,
        source_app=run.source_app,
        status=run.status.value,
        items=items,
        classification=classification,
        destinations=destinations,
        authorization=authorization,
        memory_operations=memory_operations,
        delivery=delivery,
        record_references=record_references,
        review_required=run.review_required,
        errors=[ProcessingError(**error) for error in outcome.errors],
        idempotency_key=run.idempotency_key,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _parse_metadata(operation: BrainProcessingOperation) -> dict:
    """Decode the stored item metadata of an operation."""
    if not operation.operation_metadata:
        return {}
    try:
        payload = json.loads(operation.operation_metadata)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _required_permission(metadata: dict) -> str | None:
    """Render the permission a destination needs as ``scope:action``."""
    scope = metadata.get("required_scope")
    action = metadata.get("required_action")
    if scope and action:
        return f"{scope}:{action}"
    return None


def _item_status(group: list[BrainProcessingOperation]) -> str:
    """Return the item-level status from its operations.

    An item may have several destinations. The item status reflects the best
    outcome among them, so an item that is delivered to one application while
    another destination is still unauthorized is reported as delivered. The
    per-destination states remain visible in ``delivery`` and ``authorization``.
    """
    statuses = {operation.status for operation in group}
    if OperationStatus.DELIVERED in statuses:
        return "delivered"
    if OperationStatus.READY in statuses:
        return "ready_for_delivery"
    if OperationStatus.AWAITING_REVIEW in statuses:
        return "awaiting_review"
    if OperationStatus.AWAITING_AUTHORIZATION in statuses:
        return "awaiting_authorization"
    if OperationStatus.FAILED in statuses:
        return "failed"
    if OperationStatus.SKIPPED in statuses:
        return "skipped"
    return "ready_for_delivery"


def _delivery_status(status_value: OperationStatus) -> str:
    """Map an operation status onto the public delivery status."""
    if status_value == OperationStatus.DELIVERED:
        return "delivered"
    if status_value == OperationStatus.FAILED:
        return "failed"
    if status_value == OperationStatus.SKIPPED:
        return "skipped"
    if status_value == OperationStatus.CANCELLED:
        return "cancelled"
    if status_value == OperationStatus.AWAITING_REVIEW:
        return "awaiting_review"
    if status_value == OperationStatus.AWAITING_AUTHORIZATION:
        return "awaiting_authorization"
    if status_value == OperationStatus.RETRY_PENDING:
        return "retry_pending"
    return "pending"


def _primary_destination(group: list[BrainProcessingOperation]) -> str | None:
    """Return the destination the item was actually routed to."""
    for operation in group:
        if operation.status == OperationStatus.DELIVERED:
            return operation.destination_app
    for operation in group:
        if operation.status in (
            OperationStatus.READY,
            OperationStatus.AWAITING_REVIEW,
            OperationStatus.AWAITING_AUTHORIZATION,
        ):
            return operation.destination_app
    return None


def _looks_like_object_id(operation: BrainProcessingOperation) -> bool:
    """Object-backed destinations store an object id as their reference."""
    app = get_catalog_app(operation.destination_app)
    return bool(app and app.storage_adapter == "object")


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp, tolerating malformed values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
