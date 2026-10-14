"""Schemas for the Personal Brain API boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class BrainQueryRequest(BaseModel):
    """Request sent from an application to the user's Personal Brain."""

    query: str = Field(..., min_length=1, max_length=5000)
    context: dict[str, Any] | None = None
    # Optional originating app slug. When supplied, the caller must have an
    # active connection to that app with the ``brain:read`` permission before
    # the request is forwarded to Server 2.
    source_app: str | None = Field(default=None, max_length=64)
    # Retrieval and persistence are opt-in. Conversations are never saved
    # automatically.
    use_memory: bool = False
    persist_interaction: bool = False
    memory_limit: int = Field(default=5, ge=1, le=25)


class MemoryReference(BaseModel):
    """A memory contributed to a query response."""

    id: uuid.UUID
    kind: str
    source: str
    content: str


class BrainQueryResponse(BaseModel):
    """Response returned by the Personal Brain after processing a query."""

    answer: str
    # Provider that produced the answer. Real values include ``server2_brain``,
    # ``server2_test_mode``, ``external_fallback``, or ``server2_error``; the
    # ``mock_*`` values are development-only and never indicate real inference.
    source: str = "unknown"
    context_used: bool = False
    user_id: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    tokens_used: int | None = None
    # Present only when the brain/source returned a structured failure; never
    # contains credentials or internal network details.
    error_code: str | None = None
    # Distinguishes retrieved facts from AI generated text.
    memories_used: list[MemoryReference] = Field(default_factory=list)
    persisted_memory_id: uuid.UUID | None = None


class BrainIngestRequest(BaseModel):
    """Request to ingest processed application data into the Personal Brain."""

    content: str = Field(..., min_length=1, max_length=200000)
    content_type: str = Field(default="text")
    source_app: str | None = None
    metadata: dict[str, Any] | None = None


class BrainIngestResponse(BaseModel):
    """Response for brain ingestion."""

    accepted: bool = True
    classification: dict[str, Any]
    stored: bool = True
    memory_id: uuid.UUID | None = None
    vault_entry_id: uuid.UUID | None = None


class MemoryCreateRequest(BaseModel):
    """Explicitly persist a memory for the authenticated user."""

    content: str = Field(..., min_length=1, max_length=20000)
    kind: Literal["fact", "interpretation", "event"] = "fact"
    summary: str | None = Field(default=None, max_length=512)
    source_app: str | None = None
    importance: int = Field(default=0, ge=0, le=100)


class MemoryResponse(BaseModel):
    """A persisted memory."""

    id: uuid.UUID
    ai_identity_id: uuid.UUID
    kind: str
    source: str
    content: str
    summary: str | None = None
    source_app: str | None = None
    importance: int
    created_at: datetime

    model_config = {"from_attributes": True}

class MemoryUpdateRequest(BaseModel):
    """Update an existing memory."""

    content: str | None = Field(default=None, min_length=1, max_length=20000)
    summary: str | None = Field(default=None, max_length=512)
    importance: int | None = Field(default=None, ge=0, le=100)
