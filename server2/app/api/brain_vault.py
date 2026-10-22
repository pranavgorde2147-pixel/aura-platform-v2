"""The personal vault API of one Brain.

Every endpoint is scoped to a single Brain and to the owner that Server 1
forwarded. A Brain id that belongs to another owner is reported as "not
found" - never as "forbidden" - so the API does not confirm that another
user's Brain exists.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.vault.apps import BrainAppConnectionService
from app.brain_manager.vault.service import BrainVaultService, VaultRecordNotFound
from app.security.brain import require_owner_id
from app.security.storage import require_internal_token
from app.storage_db import ManagedBrain, get_db

router = APIRouter(
    prefix="/v1/brains",
    tags=["Brain Vault"],
    dependencies=[Depends(require_internal_token)],
)


class VaultRecordCreate(BaseModel):
    record_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=512)
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    category: str | None = Field(default=None, max_length=64)
    source_app: str | None = Field(default=None, max_length=128)
    source_reference: str | None = Field(default=None, max_length=512)
    object_key: str | None = Field(default=None, max_length=1024)
    access_policy: str = Field(default="owner_only", max_length=32)


class VaultRelationshipCreate(BaseModel):
    target_record_id: uuid.UUID | None = None
    target_app: str | None = Field(default=None, max_length=128)
    relation_type: str = Field(default="related_to", max_length=64)


class VaultSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


def _record_metadata(record: Any) -> dict[str, Any]:
    return {
        "record_id": str(record.id),
        "brain_id": str(record.brain_id),
        "record_type": record.record_type,
        "category": record.category,
        "title": record.title,
        "content": record.content,
        "source_app": record.source_app,
        "source_reference": record.source_reference,
        "object_key": record.object_key,
        "access_policy": record.access_policy,
        "metadata": record.record_metadata or {},
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


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


@router.get("/{brain_id}/vault/records")
async def list_records(
    brain_id: uuid.UUID,
    record_type: str | None = Query(default=None, max_length=64),
    source_app: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List the records of one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    records, total = await vault.list_records(
        brain,
        record_type=record_type,
        source_app=source_app,
        limit=limit,
        offset=offset,
    )
    return {
        "brain_id": str(brain.brain_id),
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_record_metadata(record) for record in records],
    }


@router.post("/{brain_id}/vault/records", status_code=status.HTTP_201_CREATED)
async def create_record(
    brain_id: uuid.UUID,
    payload: VaultRecordCreate,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Store one record in the Brain's vault."""
    brain = await _resolve_brain(session, brain_id, owner_id)

    # A record may only be attributed to an app the Brain is authorized for.
    if payload.source_app:
        apps = BrainAppConnectionService(session)
        authorized = await apps.has_permission(
            brain, payload.source_app, "memory", "write"
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"App '{payload.source_app}' is not authorized to write records "
                    "on this Brain"
                ),
            )

    vault = BrainVaultService(session)
    record = await vault.put_record(
        brain,
        record_type=payload.record_type,
        title=payload.title,
        content=payload.content,
        metadata=payload.metadata,
        category=payload.category,
        source_app=payload.source_app,
        source_reference=payload.source_reference,
        object_key=payload.object_key,
        access_policy=payload.access_policy,
    )
    return _record_metadata(record)


@router.get("/{brain_id}/vault/records/{record_id}")
async def get_record(
    brain_id: uuid.UUID,
    record_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch one record of one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    try:
        record = await vault.get_record(brain, record_id)
    except VaultRecordNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Record not found"
        ) from exc
    return _record_metadata(record)


@router.delete("/{brain_id}/vault/records/{record_id}")
async def delete_record(
    brain_id: uuid.UUID,
    record_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete one record of one Brain."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    deleted = await vault.delete_record(brain, record_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Record not found"
        )
    return {"brain_id": str(brain.brain_id), "record_id": str(record_id), "deleted": True}


@router.get("/{brain_id}/vault/records/{record_id}/relationships")
async def list_relationships(
    brain_id: uuid.UUID,
    record_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List the relationships of one record."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    relationships = await vault.list_relationships(brain, record_id=record_id)
    return {
        "brain_id": str(brain.brain_id),
        "record_id": str(record_id),
        "items": [
            {
                "id": str(relationship.id),
                "source_record_id": str(relationship.source_record_id),
                "target_record_id": str(relationship.target_record_id)
                if relationship.target_record_id
                else None,
                "target_app": relationship.target_app,
                "relation_type": relationship.relation_type,
                "created_at": relationship.created_at.isoformat()
                if relationship.created_at
                else None,
            }
            for relationship in relationships
        ],
    }


@router.post(
    "/{brain_id}/vault/records/{record_id}/relationships",
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship(
    brain_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: VaultRelationshipCreate,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Link one record of one Brain to another record or to an app."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    try:
        relationship = await vault.link_records(
            brain,
            source_record_id=record_id,
            target_record_id=payload.target_record_id,
            target_app=payload.target_app,
            relation_type=payload.relation_type,
        )
    except VaultRecordNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Record not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {
        "id": str(relationship.id),
        "brain_id": str(brain.brain_id),
        "source_record_id": str(relationship.source_record_id),
        "target_record_id": str(relationship.target_record_id)
        if relationship.target_record_id
        else None,
        "target_app": relationship.target_app,
        "relation_type": relationship.relation_type,
    }


@router.post("/{brain_id}/vault/search")
async def search_records(
    brain_id: uuid.UUID,
    payload: VaultSearchRequest,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Search the Brain's own vector index."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    results = await vault.semantic_search(
        brain, payload.query, limit=payload.limit, min_score=payload.min_score
    )
    return {
        "brain_id": str(brain.brain_id),
        "query": payload.query[:200],
        "results": results,
    }


@router.get("/{brain_id}/vault/status")
async def vault_status(
    brain_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(require_owner_id),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the state of the Brain's vault and vector index."""
    brain = await _resolve_brain(session, brain_id, owner_id)
    vault = BrainVaultService(session)
    status_payload = await vault.vector_status(brain)
    return {
        "brain_id": str(brain.brain_id),
        "record_count": await vault.count_records(brain),
        "vector": status_payload,
    }


__all__ = ["router"]
