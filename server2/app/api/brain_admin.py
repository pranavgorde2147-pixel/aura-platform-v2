"""Administrative API for the Server 2 Brain registry.

These endpoints are metadata-only: they expose the state of the Brains (their
runtime, their capacity, their health) but never the content of a Brain's
vault. Access requires the shared internal service token *and* an owner id that
is on the ``BRAIN_ADMIN_OWNER_IDS`` allowlist.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.lifecycle.manager import get_runtime_manager
from app.brain_manager.monitoring.health import BrainHealthMonitor
from app.brain_manager.registry import repository
from app.brain_manager.registry.service import BrainRegistryService
from app.security.brain import require_brain_admin
from app.security.storage import require_internal_token
from app.storage_db import get_db

router = APIRouter(
    prefix="/v1/admin/brains",
    tags=["Brain Administration"],
    dependencies=[Depends(require_internal_token)],
)


def _entry(brain: Any) -> dict[str, Any]:
    return {
        "brain_id": str(brain.brain_id),
        "owner_user_id": str(brain.owner_id),
        "ai_identity_id": str(brain.ai_identity_id),
        "provisioning_status": brain.provisioning_status,
        "llm_runtime_id": brain.llm_runtime_id,
        "llm_runtime_status": brain.llm_runtime_status,
        "runtime_isolation": brain.runtime_isolation,
        "model_configuration_id": brain.model_configuration_id,
        "model_configuration": brain.model_configuration or {},
        "compute_allocation": brain.compute_allocation or {},
        "memory_resource_reference": brain.memory_resource_reference,
        "vector_resource_reference": brain.vector_resource_reference,
        "vault_resource_reference": brain.vault_resource_reference,
        "storage_resource_reference": brain.storage_resource_reference,
        "resource_root": brain.resource_root,
        "manifest_version": brain.manifest_version,
        "connected_app_count": brain.connected_app_count,
        "request_count": brain.request_count,
        "provisioning_error": brain.provisioning_error,
        "runtime_last_error": brain.runtime_last_error,
        "last_health_check": brain.last_health_check.isoformat()
        if brain.last_health_check
        else None,
        "created_at": brain.created_at.isoformat() if brain.created_at else None,
        "updated_at": brain.updated_at.isoformat() if brain.updated_at else None,
    }


@router.get("")
async def list_brains(
    provisioning_status: str | None = Query(default=None, max_length=32),
    llm_runtime_status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List the metadata of every registered Brain."""
    brains, total = await repository.list_brains(
        session,
        limit=limit,
        offset=offset,
        provisioning_status=provisioning_status,
        llm_runtime_status=llm_runtime_status,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_entry(brain) for brain in brains],
    }


@router.get("/cluster")
async def cluster_status(
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the aggregated capacity and health picture of the cluster."""
    monitor = BrainHealthMonitor(get_runtime_manager())
    return await monitor.cluster_status(session)


@router.get("/health")
async def check_all_brains(
    limit: int = Query(default=100, ge=1, le=500),
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run the health check of every registered Brain."""
    monitor = BrainHealthMonitor(get_runtime_manager())
    reports = await monitor.check_all(session, limit=limit)
    return {
        "checked": len(reports),
        "items": [report.to_dict() for report in reports],
    }


@router.get("/{brain_id}")
async def get_brain(
    brain_id: uuid.UUID,
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the registry metadata of one Brain."""
    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    return _entry(brain)


@router.get("/{brain_id}/health")
async def check_brain(
    brain_id: uuid.UUID,
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run the health check of one Brain."""
    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    monitor = BrainHealthMonitor(get_runtime_manager())
    report = await monitor.check_brain(session, brain)
    return report.to_dict()


@router.post("/reconcile")
async def reconcile(
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Reconcile the registry with the runtimes that actually exist."""
    manager = get_runtime_manager()
    report = await manager.reconcile(session)
    return {"reconciled": len(report), "items": report}


@router.post("/{brain_id}/runtime/stop")
async def stop_runtime(
    brain_id: uuid.UUID,
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Stop the dedicated runtime of one Brain."""
    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    manager = get_runtime_manager()
    released = await manager.stop_runtime(session, brain)
    return {
        "brain_id": str(brain.brain_id),
        "released": released,
        "llm_runtime_status": brain.llm_runtime_status,
    }


@router.post("/{brain_id}/runtime/restart")
async def restart_runtime(
    brain_id: uuid.UUID,
    _admin: uuid.UUID = Depends(require_brain_admin),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Restart the dedicated runtime of one Brain."""
    brain = await BrainRegistryService.get(session, brain_id)
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    manager = get_runtime_manager()
    result = await manager.restart_runtime(session, brain)
    return {
        "brain_id": str(brain.brain_id),
        "llm_runtime_id": result.runtime_id,
        "llm_runtime_status": result.status.value,
        "runtime_isolation": result.isolation,
        "error_code": result.error_code,
    }


__all__ = ["router"]
