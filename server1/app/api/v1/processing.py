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
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.models.brain_processing import BrainProcessingOperation, OperationStatus
from app.models.user import User
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
from app.services.brain_router import BrainRouter, ProcessingError as RouterProcessingError
from app.services.app_registry import get_catalog_app
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
    router_service = BrainRouter(session)
    try:
        outcome = await router_service.process(
            current_user,
            payload,
            idempotency_key=idempotency_key,
        )
    except RouterProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _build_response(outcome)


@router.get("/{processing_id}", response_model=ProcessingResponse)
async def get_processing_run(
    processing_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Return the state of one processing run owned by the caller."""
    router_service = BrainRouter(session)
    try:
        outcome = await router_service.get_run(current_user, processing_id)
    except RouterProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _build_response(outcome)


@router.post("/{processing_id}/confirm", response_model=ProcessingResponse)
async def confirm_processing_run(
    processing_id: uuid.UUID,
    payload: ConfirmRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Deliver the items the user confirmed after a review."""
    router_service = BrainRouter(session)
    try:
        outcome = await router_service.confirm(current_user, processing_id, payload.items)
    except RouterProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _build_response(outcome)


@router.post("/{processing_id}/retry", response_model=ProcessingResponse)
async def retry_processing_run(
    processing_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Retry the operations of a run that did not succeed."""
    router_service = BrainRouter(session)
    try:
        outcome = await router_service.retry(current_user, processing_id)
    except RouterProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _build_response(outcome)


@router.post("/{processing_id}/cancel", response_model=ProcessingResponse)
async def cancel_processing_run(
    processing_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProcessingResponse:
    """Cancel a run that has not been delivered yet."""
    router_service = BrainRouter(session)
    try:
        outcome = await router_service.cancel(current_user, processing_id)
    except RouterProcessingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _build_response(outcome)


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
