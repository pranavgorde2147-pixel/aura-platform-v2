"""Transport model for a Brain registry entry.

This is the shape the registry exposes to callers (Server 1, the operator
admin API). It deliberately contains **resource references and counters only**:
no memory content, no document text, no credentials, no conversation bodies.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.brain_manager.registry.enums import BrainProvisioningStatus, BrainRuntimeStatus


class BrainComputeAllocation(BaseModel):
    """The compute slice reserved for one Brain."""

    max_concurrent_requests: int = Field(default=1, ge=1)
    context_token_budget: int = Field(default=2048, ge=0)
    max_jobs_in_flight: int = Field(default=8, ge=0)
    runtime_slot: str | None = None
    enforcement: str = "per_brain_semaphore"


class BrainRegistryEntry(BaseModel):
    """One Brain row as exposed by the registry."""

    brain_id: uuid.UUID
    owner_user_id: uuid.UUID
    ai_identity_id: uuid.UUID
    provisioning_status: BrainProvisioningStatus
    llm_runtime_id: str | None = None
    llm_runtime_status: BrainRuntimeStatus
    runtime_isolation: str | None = None
    model_configuration_id: str | None = None
    compute_allocation: dict[str, Any] = Field(default_factory=dict)
    memory_resource_reference: str | None = None
    vector_resource_reference: str | None = None
    vault_resource_reference: str | None = None
    resource_root: str | None = None
    manifest_version: int = 1
    connected_app_count: int = 0
    request_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_health_check: datetime | None = None
    provisioning_error: str | None = None
    runtime_last_error: str | None = None

    model_config = {"from_attributes": True}
