"""AURA application registry and user app-connection API.

Catalog endpoints are public (read-only). Connection endpoints are protected:
every operation is scoped to the authenticated user, mirroring the ownership
rules used across the rest of the platform.
"""

from __future__ import annotations

from typing import Any

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
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
    UnknownAppError,
    get_catalog_app,
    list_catalog,
)
from app.services.personal_brain import PersonalBrainService

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
    module = await service.update_connection(
        current_user,
        module_id,
        is_active=payload.is_active,
        permissions=payload.permissions,
    )
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

    brain = PersonalBrainService(session)
    try:
        result = await brain.ingest_app_event(current_user, adapter, payload)
    except AppIngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if not result.get("accepted"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal brain not found for user",
        )
    return AppIngestResponse(source_app=adapter.slug, **result)


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