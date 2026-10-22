"""AURA application registry and user app-connection API.

Catalog endpoints are public (read-only). Connection endpoints are protected:
every operation is scoped to the authenticated user, mirroring the ownership
rules used across the rest of the platform.
"""

from __future__ import annotations

from typing import Any

import uuid
import hashlib
import json

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.models.user import User
from app.schemas.apps import (
    AppDefinition,
    AppIngestResponse,
    ConnectionCreateRequest,
    ConnectionListResponse,
    ConnectionPermissionResponse,
    ConnectionResponse,
    ConnectionUpdateRequest,
)
from app.services.app_ingestion import AppIngestionError, get_adapter
from app.services.app_registry import (
    AppAlreadyInstalledError,
    AppConnectionService,
    AppPermissionNotAllowedError,
    UnknownAppError,
    get_catalog_app,
    list_catalog,
)
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository

router = APIRouter(prefix="/apps", tags=["Apps"])


@router.get("", response_model=list[AppDefinition])
async def get_app_catalog() -> list[AppDefinition]:
    """Return the AURA application registry catalog."""
    return list_catalog()


@router.get("/me", response_model=ConnectionListResponse)
async def my_connections(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConnectionListResponse:
    """List the authenticated user's authorized app connections."""
    service = AppConnectionService(session)
    modules = await service.list_connections(current_user)
    return ConnectionListResponse(items=[_to_response(m) for m in modules])


@router.post("/me", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def connect_app(
    payload: ConnectionCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Authorize an app from the catalog for the authenticated user."""
    service = AppConnectionService(session)
    try:
        module = await service.install(
            current_user,
            payload.module_slug,
            permissions=payload.permissions,
        )
    except UnknownAppError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AppAlreadyInstalledError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AppPermissionNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return _to_response(module)


@router.get("/me/{module_id}", response_model=ConnectionResponse)
async def get_connection(
    module_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Return one of the user's app connections."""
    service = AppConnectionService(session)
    module = await service.get_connection(current_user, module_id)
    if module is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return _to_response(module)


@router.patch("/me/{module_id}", response_model=ConnectionResponse)
@router.put("/me/{module_id}", response_model=ConnectionResponse)
async def update_connection(
    module_id: uuid.UUID,
    payload: ConnectionUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConnectionResponse:
    """Update activation state and/or permissions for a connection."""
    service = AppConnectionService(session)
    try:
        module = await service.update_connection(
            current_user,
            module_id,
            is_active=payload.is_active,
            permissions=payload.permissions,
        )
    except AppPermissionNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if module is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return _to_response(module)


@router.delete("/me/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connection(
    module_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Revoke the authenticated user's access to an app."""
    service = AppConnectionService(session)
    revoked = await service.revoke(current_user, module_id)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return None


@router.post(
    "/{slug}/ingest",
    response_model=AppIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_app_event(
    slug: str,
    payload: dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AppIngestResponse:
    """Route one authorized application event into the user's Personal Brain.

    The authenticated user is resolved server-side from the bearer token; the
    payload never names an owner. The connection's active permission for the
    app's declared scope/action is enforced before any data is written.
    """
    adapter = get_adapter(slug)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ingestion integration exists for this app",
        )

    connections = AppConnectionService(session)
    authorized = await connections.has_active_permission(
        current_user, adapter.slug, adapter.scope, adapter.action
    )
    if not authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="App is not authorized to send data to this user's brain",
        )

    try:
        normalized = adapter.normalize(payload)
    except AppIngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    identity = await AIIdentityRepository(session).get_by_user_id(current_user.id)
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity.id)
        if identity
        else None
    )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal Brain is not ready",
        )
    metadata = {
        key: value for key, value in normalized.metadata.items() if key != "raw"
    }
    metadata["labels"] = list(normalized.labels)
    metadata["importance"] = normalized.importance
    metadata["kind"] = normalized.kind.value if normalized.kind is not None else None
    metadata["event_type"] = normalized.event_type
    if normalized.summary is not None:
        metadata["summary"] = normalized.summary
    canonical = json.dumps(
        {
            "source_app": adapter.slug,
            "dedupe_key": normalized.dedupe_key,
            "event_type": normalized.event_type,
            "content": normalized.content,
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    idempotency_key = normalized.dedupe_key or hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    try:
        stored = await get_brain_client().vault_request(
            user_id=str(current_user.id),
            brain_id=str(brain.id),
            method="POST",
            path="records",
            json_body={
                "record_type": normalized.resource_type or normalized.event_type,
                "namespace": f"apps/{adapter.slug}",
                "idempotency_key": idempotency_key,
                "title": normalized.summary,
                "content": normalized.content,
                "source_app": adapter.slug,
                "source_reference": normalized.dedupe_key,
                "category": normalized.event_type[:64],
                "metadata": {**metadata, "is_original": True},
            },
        )
    except BrainServiceError as exc:
        if exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key was already used for different data.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal Brain storage is unavailable; retry synchronization.",
        ) from exc
    record_id = stored.get("record_id")
    return AppIngestResponse(
        source_app=adapter.slug,
        event_type=normalized.event_type,
        accepted=True,
        duplicate=False,
        partial=False,
        memory_id=record_id if normalized.persist_memory else None,
        vault_entry_id=record_id,
        record_id=record_id,
    )


@router.get("/{slug}", response_model=AppDefinition, include_in_schema=True)
async def get_app(slug: str) -> AppDefinition:
    """Return one registry entry."""
    app = get_catalog_app(slug)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown app slug")
    return app


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _to_response(module) -> ConnectionResponse:
    return ConnectionResponse(
        id=module.id,
        module_slug=module.module_slug,
        module_name=module.module_name,
        platform_type=_enum_value(module.platform_type),
        is_active=module.is_active,
        created_at=module.created_at,
        updated_at=module.updated_at,
        permissions=[
            ConnectionPermissionResponse(
                id=permission.id,
                scope=permission.scope,
                action=_enum_value(permission.permission),
            )
            for permission in module.module_permissions
        ],
    )