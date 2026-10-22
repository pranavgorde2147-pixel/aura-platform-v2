"""Service-authenticated registry for owner-isolated Brain resources.

Server 1 authenticates the user and asserts the canonical Brain identity
(``brain_id``, ``owner_id``, ``ai_identity_id``). Server 2 registers it, gives
the Brain its dedicated runtime and its private resources, and reports the
result honestly: a Brain whose runtime could not be created is marked as
failed, never quietly served by somebody else's runtime.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.lifecycle.manager import get_runtime_manager
from app.brain_manager.provisioning.orchestrator import BrainProvisioningOrchestrator
from app.brain_manager.registry.enums import BrainProvisioningStatus, BrainRuntimeStatus
from app.brain_manager.registry.service import BrainRegistryConflict, BrainRegistryService
from app.security.storage import require_internal_token
from app.storage_db import ManagedBrain, get_db

router = APIRouter(
    prefix="/v1/brains",
    tags=["Brain Registry"],
    dependencies=[Depends(require_internal_token)],
)


class BrainProvisionRequest(BaseModel):
    brain_id: uuid.UUID
    owner_id: uuid.UUID
    ai_identity_id: uuid.UUID
    model_id: str = Field(min_length=1, max_length=128)
    resource_manifest_version: int = Field(default=1, ge=1)
    max_concurrent_requests: int = Field(default=1, ge=1, le=8)


class BrainRegistryResponse(BaseModel):
    brain_id: uuid.UUID
    owner_id: uuid.UUID
    ai_identity_id: uuid.UUID
    provisioning_status: str
    inference_runtime_status: str
    model_configuration: dict
    compute_allocation: dict
    storage_resource_reference: str
    request_count: int
    last_health_check: datetime | None
    llm_runtime_id: str | None = None
    llm_runtime_status: str = "unregistered"
    runtime_isolation: str | None = None
    model_configuration_id: str | None = None
    memory_resource_reference: str | None = None
    vector_resource_reference: str | None = None
    vault_resource_reference: str | None = None
    resource_root: str | None = None
    manifest_version: int = 1
    connected_app_count: int = 0
    provisioning_error: str | None = None
    runtime_last_error: str | None = None


def _response(brain: ManagedBrain) -> BrainRegistryResponse:
    return BrainRegistryResponse(
        brain_id=brain.brain_id,
        owner_id=brain.owner_id,
        ai_identity_id=brain.ai_identity_id,
        provisioning_status=brain.provisioning_status,
        inference_runtime_status=brain.llm_runtime_status,
        model_configuration=brain.model_configuration,
        compute_allocation=brain.compute_allocation,
        storage_resource_reference=brain.storage_resource_reference,
        request_count=brain.request_count,
        last_health_check=brain.last_health_check,
        llm_runtime_id=brain.llm_runtime_id,
        llm_runtime_status=brain.llm_runtime_status,
        runtime_isolation=brain.runtime_isolation,
        model_configuration_id=brain.model_configuration_id,
        memory_resource_reference=brain.memory_resource_reference,
        vector_resource_reference=brain.vector_resource_reference,
        vault_resource_reference=brain.vault_resource_reference,
        resource_root=brain.resource_root,
        manifest_version=brain.manifest_version,
        connected_app_count=brain.connected_app_count,
        provisioning_error=brain.provisioning_error,
        runtime_last_error=brain.runtime_last_error,
    )


def _available_model_ids() -> set[str]:
    """Return the model identifiers this service can actually serve."""
    configured = os.getenv("MODEL_NAME", "Qwen3.5-0.8B")
    valid = {configured}
    external_model = os.getenv("EXTERNAL_MODEL", "").strip()
    if external_model:
        valid.add(external_model)
    return valid


@router.post("/provision", response_model=BrainRegistryResponse)
async def provision_brain(
    payload: BrainProvisionRequest,
    session: AsyncSession = Depends(get_db),
) -> BrainRegistryResponse:
    """Idempotently register and provision a primary Brain asserted by Server 1."""
    if payload.model_id not in _available_model_ids():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Requested model is not available on this Brain service",
        )

    model_configuration: dict[str, Any] = {
        "model_id": payload.model_id,
        "runtime_model_id": os.getenv("MODEL_NAME", "Qwen3.5-0.8B"),
    }
    compute_allocation = {
        "max_concurrent_requests": payload.max_concurrent_requests,
        "concurrency_enforcement": "per-brain",
    }

    orchestrator = BrainProvisioningOrchestrator(get_runtime_manager())
    try:
        outcome = await orchestrator.provision(
            session,
            brain_id=payload.brain_id,
            owner_id=payload.owner_id,
            ai_identity_id=payload.ai_identity_id,
            model_configuration=model_configuration,
            compute_allocation=compute_allocation,
            manifest_version=payload.resource_manifest_version,
        )
    except BrainRegistryConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    brain = await BrainRegistryService.get(session, payload.brain_id)
    if brain is None:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The Brain could not be registered",
        )
    return _response(brain)


@router.get("/{brain_id}", response_model=BrainRegistryResponse)
async def get_brain(
    brain_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> BrainRegistryResponse:
    """Return the registry entry of one Brain (metadata only)."""
    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    return _response(brain)


@router.get("/{brain_id}/manifest")
async def get_manifest(
    brain_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the resource manifest of one Brain."""
    from app.brain_manager.db_models import BrainManifestRecord

    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    result = await session.execute(
        select(BrainManifestRecord).where(BrainManifestRecord.brain_id == brain_id)
    )
    record = result.scalars().first()
    return {
        "brain_id": str(brain.brain_id),
        "manifest_version": brain.manifest_version,
        "manifest": record.manifest if record is not None else {},
    }


async def get_ready_brain(
    session: AsyncSession, owner_id: uuid.UUID
) -> ManagedBrain | None:
    """Return the Brain of one owner when it is ready to serve requests."""
    result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.owner_id == owner_id,
            ManagedBrain.provisioning_status == BrainProvisioningStatus.READY.value,
        )
    )
    return result.scalars().first()


async def record_brain_request(session: AsyncSession, brain: ManagedBrain) -> None:
    """Record one inference request against a Brain."""
    brain.request_count = int(brain.request_count or 0) + 1
    brain.last_health_check = datetime.now(timezone.utc)
    await session.commit()


__all__ = [
    "BrainRegistryResponse",
    "BrainRuntimeStatus",
    "get_ready_brain",
    "record_brain_request",
    "router",
]
