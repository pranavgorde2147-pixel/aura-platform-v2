"""Connected-app API for one Brain.

Server 1 owns the catalog and the consent decisions; these endpoints mirror an
authenticated consent decision into the Brain's authorization snapshot and
revoke it again.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.vault.apps import (
    CONSENT_GRANTED,
    CONSENT_REVOKED,
    BrainAppConnectionService,
    connection_metadata,
)
from app.security.brain import require_owner_id
from app.security.storage import require_internal_token
from app.storage_db import ManagedBrain, get_db

router = APIRouter(
    prefix="/v1/brains",
    tags=["Brain Apps"],
    dependencies=[Depends(require_internal_token)],
)


class AppScope(BaseModel):
    scope: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)


class AppConnectionRequest(BaseModel):
    app_slug: str = Field(min_length=1, max_length=128)
    granted_scopes: list[AppScope] = Field(default_factory=list)
    is_active: bool = True
    consent_state: str = Field(default=CONSENT_GRANTED, max_length=32)


async def _resolve_brain(
    session: AsyncSession, brain_id: uuid.UUID, owner_id: uuid.UUID
) -> ManagedBrain:
    brain = await BrainRegistryService.get_owned(
        session, brain_id=brain_id, owner_id=owner_id
    )
    if brain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brain not found"
        )
    return brain


@router.get("/{brain_id}/apps")
async def list_apps(
    brain_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List the connected apps of one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    apps = BrainAppConnectionService(session)
    connections = await apps.list_connections(brain)
    return {
        "brain_id": str(brain.brain_id),
        "connected_app_count": len(connections),
        "items": [connection_metadata(connection) for connection in connections],
    }


@router.put("/{brain_id}/apps", status_code=status.HTTP_200_OK)
async def set_app_connection(
    brain_id: uuid.UUID,
    payload: AppConnectionRequest,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record (or update) the consent snapshot of one app."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    apps = BrainAppConnectionService(session)
    try:
        connection = await apps.set_connection(
            brain,
            app_slug=payload.app_slug,
            granted_scopes=[scope.model_dump() for scope in payload.granted_scopes],
            is_active=payload.is_active,
            consent_state=payload.consent_state,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {
        "brain_id": str(brain.brain_id),
        "connected_app_count": await apps.connected_app_count(brain),
        "connection": connection_metadata(connection),
    }


@router.delete("/{brain_id}/apps/{app_slug}")
async def revoke_app(
    brain_id: uuid.UUID,
    app_slug: str,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Revoke the consent snapshot of one app."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    apps = BrainAppConnectionService(session)
    revoked = await apps.revoke_connection(brain, app_slug)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="App connection not found"
        )
    return {
        "brain_id": str(brain.brain_id),
        "app_slug": app_slug,
        "consent_state": CONSENT_REVOKED,
        "connected_app_count": await apps.connected_app_count(brain),
    }


__all__ = ["router"]
