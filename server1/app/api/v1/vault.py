"""Authenticated gateway for the authenticated user's Server 2 Brain vault."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.services.app_registry import AppConnectionService, get_catalog_app

router = APIRouter(prefix="/brain/vault", tags=["Brain Vault"])


class VaultRecordCreate(BaseModel):
    client_app: str | None = Field(default=None, max_length=100)
    source_app: str = Field(min_length=1, max_length=100)
    record_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=512)
    content: str | None = Field(default=None, max_length=200000)
    summary: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=64)
    source_reference: str | None = Field(default=None, max_length=512)
    idempotency_key: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VaultRecordUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=512)
    summary: str | None = Field(default=None, max_length=4000)
    content: str | None = Field(default=None, max_length=200000)
    metadata: dict[str, Any] | None = None
    confirm_original_edit: bool = False


class VaultSearchRequest(BaseModel):
    source_app: str
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=50)


async def _owned_brain(session: AsyncSession, user: User):
    identity = await AIIdentityRepository(session).get_by_user_id(user.id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Personal Brain not found")
    brain = await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(status_code=503, detail="Personal Brain is not ready")
    return identity, brain


async def _require_app_permission(
    session: AsyncSession, user: User, slug: str, scope: str, action: str
) -> None:
    if get_catalog_app(slug) is None:
        raise HTTPException(status_code=404, detail="Unknown application")
    authorized = await AppConnectionService(session).has_active_permission(
        user, slug, scope, action
    )
    if not authorized:
        raise HTTPException(status_code=403, detail="Application permission is required")


async def _authorize_record_action(
    session: AsyncSession,
    user: User,
    *,
    client_app: str | None,
    source_app: str,
    action: str,
) -> None:
    caller = client_app or source_app
    if caller == source_app:
        await _require_app_permission(
            session,
            user,
            caller,
            "files" if caller == "files" else (
                "memory" if action == "write" else "brain"
            ),
            action,
        )
        return

    await _require_app_permission(
        session,
        user,
        caller,
        "memory" if action == "write" else "brain",
        action,
    )
    if source_app != "shared":
        await _require_app_permission(
            session,
            user,
            source_app,
            "files" if source_app == "files" else (
                "memory" if action == "write" else "brain"
            ),
            action,
        )


def _granted_namespaces(caller_app: str, modules: list) -> list[str]:
    """Return namespaces from active connections with explicit read grants."""
    namespaces = {"shared"}
    for module in modules:
        if not module.is_active:
            continue
        if AppConnectionService.has_permission(module, "brain", "read"):
            namespaces.add(f"apps/{module.module_slug}")
        if module.module_slug == "files" and AppConnectionService.has_permission(
            module, "files", "read"
        ):
            namespaces.add("documents")
    namespaces.add("conversations")
    return sorted(namespaces)


async def _readable_namespaces(
    session: AsyncSession, user: User, caller_app: str
) -> list[str]:
    """Limit cross-app retrieval to this user's active, explicitly granted apps."""
    modules = await AppConnectionService(session).list_connections(user)
    return _granted_namespaces(caller_app, modules)


async def _call_server2(user_id: uuid.UUID, brain_id: uuid.UUID, **kwargs):
    try:
        return await get_brain_client().vault_request(
            user_id=str(user_id), brain_id=str(brain_id), **kwargs
        )
    except BrainServiceError as exc:
        if exc.status_code == 409:
            raise HTTPException(status_code=409, detail="Brain record conflict") from exc
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Brain record not found") from exc
        raise HTTPException(
            status_code=503,
            detail="Personal Brain storage is unavailable",
        ) from exc


async def _owned_app_record(
    user_id: uuid.UUID,
    brain_id: uuid.UUID,
    record_id: uuid.UUID,
    source_app: str,
) -> dict[str, Any]:
    record = await _call_server2(
        user_id,
        brain_id,
        method="GET",
        path=f"records/{record_id}",
    )
    expected_namespace = "documents" if source_app == "files" else f"apps/{source_app}"
    if (
        record.get("source_app") != source_app
        or record.get("namespace") != expected_namespace
    ):
        raise HTTPException(status_code=404, detail="Brain record not found")
    return record


@router.post("/records", status_code=status.HTTP_201_CREATED)
async def create_record(
    payload: VaultRecordCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Write one app record to this user's unified Brain vault."""
    await _authorize_record_action(
        session,
        current_user,
        client_app=payload.client_app,
        source_app=payload.source_app,
        action="write",
    )
    _, brain = await _owned_brain(session, current_user)
    metadata = dict(payload.metadata)
    metadata["is_original"] = True
    if payload.summary is not None:
        metadata["summary"] = payload.summary
    return await _call_server2(
        current_user.id,
        brain.id,
        method="POST",
        path="records",
        json_body={
            "record_type": payload.record_type,
            "namespace": (
                "documents" if payload.source_app == "files"
                else f"apps/{payload.source_app}"
            ),
            "idempotency_key": payload.idempotency_key,
            "title": payload.title,
            "content": payload.content,
            "source_app": payload.source_app,
            "source_reference": payload.source_reference,
            "category": payload.category,
            "metadata": metadata,
        },
    )


@router.get("/records")
async def list_records(
    source_app: str,
    client_app: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List one authorized namespace from the user's Brain vault."""
    await _authorize_record_action(
        session,
        current_user,
        client_app=client_app,
        source_app=source_app,
        action="read",
    )
    _, brain = await _owned_brain(session, current_user)
    namespace = "documents" if source_app == "files" else f"apps/{source_app}"
    return await _call_server2(
        current_user.id,
        brain.id,
        method="GET",
        path="records",
        params={"namespace": namespace, "limit": limit, "offset": offset},
    )


@router.get("/records/{record_id}")
async def get_record(
    record_id: uuid.UUID,
    source_app: str,
    client_app: str | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch one record from an authorized namespace of the user's Brain."""
    await _authorize_record_action(
        session,
        current_user,
        client_app=client_app,
        source_app=source_app,
        action="read",
    )
    _, brain = await _owned_brain(session, current_user)
    return await _owned_app_record(current_user.id, brain.id, record_id, source_app)


@router.post("/search")
async def search_records(
    payload: VaultSearchRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Search namespaces authorized by this user's active app connections."""
    await _require_app_permission(
        session,
        current_user,
        payload.source_app,
        "files" if payload.source_app == "files" else "brain",
        "read",
    )
    _, brain = await _owned_brain(session, current_user)
    return await _call_server2(
        current_user.id,
        brain.id,
        method="POST",
        path="search",
        json_body={
            "query": payload.query,
            "limit": payload.limit,
            "namespaces": await _readable_namespaces(
                session, current_user, payload.source_app
            ),
        },
    )


@router.put("/records/{record_id}")
async def update_record(
    record_id: uuid.UUID,
    payload: VaultRecordUpdate,
    source_app: str,
    client_app: str | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Version-check updates; original content changes require explicit consent."""
    await _authorize_record_action(
        session,
        current_user,
        client_app=client_app,
        source_app=source_app,
        action="write",
    )
    _, brain = await _owned_brain(session, current_user)
    await _owned_app_record(current_user.id, brain.id, record_id, source_app)
    return await _call_server2(
        current_user.id,
        brain.id,
        method="PUT",
        path=f"records/{record_id}",
        json_body=payload.model_dump(),
    )


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    record_id: uuid.UUID,
    source_app: str,
    client_app: str | None = None,
    confirm: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete an app record only after explicit owner confirmation."""
    if not confirm:
        raise HTTPException(status_code=409, detail="Explicit delete confirmation is required")
    await _authorize_record_action(
        session,
        current_user,
        client_app=client_app,
        source_app=source_app,
        action="write",
    )
    _, brain = await _owned_brain(session, current_user)
    await _owned_app_record(current_user.id, brain.id, record_id, source_app)
    await _call_server2(
        current_user.id,
        brain.id,
        method="DELETE",
        path=f"records/{record_id}",
        params={"confirm": "true"},
    )
