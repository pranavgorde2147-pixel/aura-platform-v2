"""Schemas for the universal Brain processing pipeline.

The pipeline is the shared understanding-and-organisation layer every AURA
application talks to. This module defines its request and response contracts.

Design rules that the schemas enforce structurally:

* the authoritative user identity is **never** accepted from the client — it
  is derived from the authenticated context by the API layer;
* the client-supplied ``source_app`` and ``requested_destinations`` are
  treated as untrusted hints and are validated against the server-side
  registry and permission system before anything is written;
* responses never contain credentials, internal network details or hidden
  reasoning traces.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ProcessingStatusLiteral = Literal[
    "received",
    "validated",
    "processing",
    "classified",
    "awaiting_review",
    "ready_for_delivery",
    "delivering",
    "partially_delivered",
    "delivered",
    "retry_pending",
    "failed",
    "cancelled",
]

ItemStatusLiteral = Literal[
    "ready_for_delivery",
    "awaiting_review",
    "awaiting_authorization",
    "delivered",
    "skipped",
    "failed",
    "rejected",
]

DeliveryStatusLiteral = Literal[
    "pending",
    "delivered",
    "failed",
    "skipped",
    "retry_pending",
    "awaiting_authorization",
    "awaiting_review",
    "cancelled",
]


class ConsentOptions(BaseModel):
    """Consent and processing preferences supplied with an input."""

    # Whether the user allows this input to contribute to Brain memory.
    memory_write: bool = False
    # Whether the original content may be retained in object storage.
    retain_source: bool = True
    # Whether records may be routed to applications other than the source.
    cross_app_routing: bool = False


class ProcessingRequest(BaseModel):
    """One input submitted to the Brain processing pipeline."""

    content: str = Field(..., min_length=1, max_length=200000)
    # Stable client-side identifier. Used for provenance and for correlating
    # retries; it is not proof of identity.
    input_id: str | None = Field(default=None, max_length=255)
    input_type: str = Field(default="text", max_length=64)
    # The application submitting the input. Validated server-side.
    source_app: str | None = Field(default=None, max_length=100)
    # A protected reference to the original content (object id, upstream
    # record id). When present the raw content is not duplicated.
    content_ref: str | None = Field(default=None, max_length=1024)
    occurred_at: datetime | None = None
    captured_at: datetime | None = None
    language: str | None = Field(default=None, max_length=32)
    session_id: str | None = Field(default=None, max_length=255)
    related_record_ids: list[str] = Field(default_factory=list, max_length=100)
    context: dict[str, Any] = Field(default_factory=dict)
    consent: ConsentOptions = Field(default_factory=ConsentOptions)
    # Memory retrieval is opt-in per request and always bounded.
    use_memory: bool = True
    memory_limit: int = Field(default=5, ge=1, le=25)
    # Memory *writes* stay opt-in unless the input explicitly asks to remember
    # something and the policy allows it.
    persist_memory: bool = False
    # Explicit user instruction: only these destinations may be used.
    requested_destinations: list[str] = Field(default_factory=list, max_length=20)
    # Body-level alternative to the ``Idempotency-Key`` header.
    idempotency_key: str | None = Field(default=None, max_length=255)


class ProcessedItem(BaseModel):
    """One extracted, classified piece of an input."""

    index: int
    category: str
    record_type: str
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    # ``model`` or ``heuristic``. Heuristic scores are not calibrated.
    confidence_kind: str = "heuristic"
    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    # Reference back to the original input (run id or supplied reference).
    source_reference: str | None = None
    destination_app: str | None = None
    status: ItemStatusLiteral = "ready_for_delivery"
    requires_review: bool = False
    rationale: str | None = None


class AuthorizationOutcome(BaseModel):
    """The authorization decision for one proposed destination."""

    destination_app: str
    record_type: str
    allowed: bool
    reason: str | None = None
    required_scope: str | None = None
    required_action: str | None = None
    required_permission: str | None = None


class DestinationDecision(BaseModel):
    """The destination chosen for one item, plus alternatives when unsure."""

    destination_app: str | None = None
    record_type: str
    category: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: str
    # Alternative destinations that would also be acceptable.
    candidates: list[str] = Field(default_factory=list)


class MemoryOperation(BaseModel):
    """What happened to Brain memory for one item."""

    action: Literal["stored", "skipped", "duplicate", "denied", "failed"]
    item_index: int | None = None
    memory_id: str | None = None
    kind: str | None = None
    reason: str | None = None
    # Bounded preview of what was (or would be) remembered.
    content_preview: str | None = None


class DeliveryOutcome(BaseModel):
    """The delivery result for one destination operation."""

    destination_app: str
    record_type: str
    status: DeliveryStatusLiteral
    record_reference: str | None = None
    error_code: str | None = None
    attempts: int = 0


class RecordReference(BaseModel):
    """A confirmed stored record."""

    destination_app: str
    record_type: str
    record_id: str
    storage_type: str


class ProcessingError(BaseModel):
    """A safe error description. Never contains credentials or raw content."""

    code: str
    message: str
    stage: str | None = None


class ClassificationSummary(BaseModel):
    """Aggregate view of the classification performed for an input."""

    analysis_mode: str = "deterministic"
    confidence_kind: str = "heuristic"
    segment_count: int = 0
    categories: list[str] = Field(default_factory=list)
    # The configured confidence threshold that applied to each item.
    thresholds: dict[str, float] = Field(default_factory=dict)


class ProcessingResponse(BaseModel):
    """The structured result of one processing run."""

    input_id: str
    processing_id: uuid.UUID
    source_app: str
    status: ProcessingStatusLiteral
    items: list[ProcessedItem] = Field(default_factory=list)
    classification: ClassificationSummary = Field(default_factory=ClassificationSummary)
    destinations: list[DestinationDecision] = Field(default_factory=list)
    authorization: list[AuthorizationOutcome] = Field(default_factory=list)
    memory_operations: list[MemoryOperation] = Field(default_factory=list)
    delivery: list[DeliveryOutcome] = Field(default_factory=list)
    record_references: list[RecordReference] = Field(default_factory=list)
    review_required: bool = False
    errors: list[ProcessingError] = Field(default_factory=list)
    idempotency_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConfirmItem(BaseModel):
    """One item the user confirms for delivery."""

    index: int
    # Optional override of the destination chosen by the Brain.
    destination_app: str | None = Field(default=None, max_length=100)
    record_type: str | None = Field(default=None, max_length=100)


class ConfirmRequest(BaseModel):
    """Confirm previously reviewed items for delivery."""

    items: list[ConfirmItem] = Field(default_factory=list, max_length=50)
