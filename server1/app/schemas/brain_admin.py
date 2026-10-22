"""Metadata-only administrative views of provisioned Brains."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BrainAdminItem(BaseModel):
    brain_id: uuid.UUID
    owner_user_id: uuid.UUID
    ai_identity_id: uuid.UUID
    provisioning_status: str
    created_at: datetime
    updated_at: datetime
    inference_runtime_status: str
    model_configuration_id: str
    compute_allocation: dict[str, int | str]
    inference_request_count: int
    storage_resource_reference: str | None
    storage_bytes_relational: int
    connected_app_count: int
    memory_retrieval_status: str
    recent_processing_job_status: str | None
    last_health_check: datetime | None
    provisioning_error: str | None
    # Dedicated LLM runtime assigned to this Brain by Server 2, and the honest
    # isolation level that runtime provides.
    llm_runtime_id: str | None = None
    runtime_isolation: str | None = None


class BrainAdminListResponse(BaseModel):
    total: int
    provisioning_status_counts: dict[str, int]
    limit: int
    offset: int
    items: list[BrainAdminItem]
