"""Authenticated shared-record API backed by Server 1's canonical app_records."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.storage_client import StorageServiceError, get_storage_client
from app.models.app_record import AppRecordLink
from app.models.user import User
from app.repositories.app_record import AppRecordRepository
from app.schemas.records import (
    AppRecordLinkCreateRequest,
    AppRecordLinkResponse,
    AppRecordResponse,
)
from app.services.app_registry import (
    AppConnectionService,
    get_destination_permission,
    get_catalog_app,
)

router = APIRouter(prefix="/records", tags=["Shared Records"])


async def _readable_source_apps(
    user: User,
    client_app: str,
    session: AsyncSession,
    *,
    action: str = "read",
) -> set[str]:
    """Require the client app's scoped grant and return readable sources."""
    if get_catalog_app(client_app) is None:
        raise HTTPException(status_code=404, detail="Unknown client app")
    connections = AppConnectionService(session)
    if not await connections.has_active_permission(
        user, client_app, scope="records", action=action
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The app is not authorized for records:{action}",
        )
    modules = await connections.list_connections(user)
    return {
        module.module_slug
        for module in modules
        if module.is_active
        and AppConnectionService.has_permission(
            module, module.module_slug, "read"
        )
    }


def _record_response(record, *, include_content: bool = False) -> AppRecordResponse:
    return AppRecordResponse(
        id=record.id,
        source_app=record.destination_app,
        record_type=record.record_type,
        title=record.title,
        summary=record.summary,
        content=record.content if include_content else None,
        storage_type=record.storage_type,
        original_content_reference=record.object_reference,
        source_reference=record.source_reference,
        categories=list(record.categories or []),
        topics=list(record.topics or []),
        metadata=dict(record.record_metadata or {}),
        access_policy=record.access_policy,
        processing_status=record.processing_status,
        version=record.version,
        occurred_at=record.occurred_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _link_response(link) -> AppRecordLinkResponse:
    return AppRecordLinkResponse(
        id=link.id,
        record_id=link.record_id,
        related_record_id=link.related_record_id,
        target_app=link.target_app,
        relationship=link.relation_type,
        created_at=link.created_at,
    )


@router.get("", response_model=list[AppRecordResponse])
async def list_records(
    client_app: str = Query(..., min_length=1, max_length=100),
    source_app: str | None = Query(default=None, max_length=100),
    destination_app: str | None = Query(default=None, max_length=100),
    record_type: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=64),
    topic: str | None = Query(default=None, max_length=200),
    include_content: bool = False,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[AppRecordResponse]:
    """List records from source apps this user explicitly authorized for reads."""
    readable_sources = await _readable_source_apps(
        current_user, client_app, session
    )
    if source_app is not None and source_app not in readable_sources:
        raise HTTPException(status_code=403, detail="The source app is not authorized for reading")
    if destination_app is not None and destination_app not in readable_sources:
        raise HTTPException(status_code=403, detail="The destination app is not authorized for reading")
    if occurred_after and occurred_before and occurred_after > occurred_before:
        raise HTTPException(status_code=422, detail="occurred_after must not be later than occurred_before")
    records = await AppRecordRepository(session).list_for_user(
        user_id=current_user.id,
        source_apps={source_app} if source_app else readable_sources,
        record_type=record_type,
        category=category,
        topic=topic,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        target_app=destination_app,
        limit=limit,
        offset=offset,
    )
    return [_record_response(record, include_content=include_content) for record in records]


@router.get("/search", response_model=list[AppRecordResponse])
async def search_records(
    q: str = Query(..., min_length=1, max_length=500),
    client_app: str = Query(..., min_length=1, max_length=100),
    source_app: str | None = Query(default=None, max_length=100),
    record_type: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=64),
    topic: str | None = Query(default=None, max_length=200),
    include_content: bool = False,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[AppRecordResponse]:
    """Search content and structured metadata within explicitly readable sources."""
    readable_sources = await _readable_source_apps(
        current_user, client_app, session
    )
    if source_app is not None and source_app not in readable_sources:
        raise HTTPException(status_code=403, detail="The source app is not authorized for reading")
    if occurred_after and occurred_before and occurred_after > occurred_before:
        raise HTTPException(status_code=422, detail="occurred_after must not be later than occurred_before")
    records = await AppRecordRepository(session).search_for_user(
        user_id=current_user.id,
        query=q,
        limit=limit,
        source_apps={source_app} if source_app else readable_sources,
        record_type=record_type,
        category=category,
        topic=topic,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )
    return [_record_response(record, include_content=include_content) for record in records]


@router.get("/{record_id}", response_model=AppRecordResponse)
async def get_record(
    record_id: uuid.UUID,
    client_app: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AppRecordResponse:
    """Retrieve one owner-scoped record after live connection checks."""
    readable_sources = await _readable_source_apps(
        current_user, client_app, session
    )
    record = await AppRecordRepository(session).get_for_user(
        user_id=current_user.id, record_id=record_id
    )
    if record is None or record.destination_app not in readable_sources:
        raise HTTPException(status_code=404, detail="Record not found")
    return _record_response(record, include_content=True)


@router.get("/{record_id}/related", response_model=list[AppRecordLinkResponse])
async def get_related_records(
    record_id: uuid.UUID,
    client_app: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[AppRecordLinkResponse]:
    """Return only links whose target remains authorized and owner-accessible."""
    readable_sources = await _readable_source_apps(
        current_user, client_app, session
    )
    repository = AppRecordRepository(session)
    record = await repository.get_for_user(user_id=current_user.id, record_id=record_id)
    if record is None or record.destination_app not in readable_sources:
        raise HTTPException(status_code=404, detail="Record not found")
    links = await repository.links_for_record(user_id=current_user.id, record_id=record_id)
    visible = []
    for link in links:
        if link.target_app is not None:
            if link.target_app in readable_sources:
                visible.append(_link_response(link))
            continue
        related = await repository.get_for_user(
            user_id=current_user.id, record_id=link.related_record_id
        )
        if related is not None and related.destination_app in readable_sources:
            visible.append(_link_response(link))
    return visible


@router.put("/{record_id}/links", response_model=AppRecordLinkResponse)
async def link_record(
    record_id: uuid.UUID,
    payload: AppRecordLinkCreateRequest,
    client_app: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AppRecordLinkResponse:
    """Create an idempotent reference without copying canonical record content."""
    readable_sources = await _readable_source_apps(
        current_user, client_app, session, action="write"
    )
    repository = AppRecordRepository(session)
    record = await repository.get_for_user(user_id=current_user.id, record_id=record_id)
    if record is None or record.destination_app not in readable_sources:
        raise HTTPException(status_code=404, detail="Record not found")

    if payload.target_app:
        target = get_catalog_app(payload.target_app)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown target app")
        target_permission = get_destination_permission(target.slug)
        if (
            not target.routable
            or target.storage_adapter == "none"
            or target_permission is None
            or not await AppConnectionService(session).has_active_permission(
                current_user,
                target.slug,
                scope=target_permission[0],
                action=target_permission[1],
            )
        ):
            raise HTTPException(status_code=403, detail="Target app is not authorized for this record")
        existing = await repository.find_app_link(
            user_id=current_user.id,
            record_id=record.id,
            target_app=target.slug,
            relation_type=payload.relationship,
        )
        link = existing or AppRecordLink(
            user_id=current_user.id,
            record_id=record.id,
            target_app=target.slug,
            relation_type=payload.relationship,
        )
    else:
        if payload.related_record_id == record.id:
            raise HTTPException(status_code=422, detail="A record cannot be linked to itself")
        related = await repository.get_for_user(
            user_id=current_user.id,
            record_id=payload.related_record_id,
        )
        if related is None or related.destination_app not in readable_sources:
            raise HTTPException(status_code=404, detail="Related record not found")
        existing = await repository.find_record_link(
            user_id=current_user.id,
            record_id=record.id,
            related_record_id=related.id,
            relation_type=payload.relationship,
        )
        link = existing or AppRecordLink(
            user_id=current_user.id,
            record_id=record.id,
            related_record_id=related.id,
            relation_type=payload.relationship,
        )

    if existing is not None:
        return _link_response(existing)
    session.add(link)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="The record link conflicts with an existing link") from exc
    return _link_response(link)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    record_id: uuid.UUID,
    client_app: str = Query(..., min_length=1, max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Delete an owned record only when both vault and source-app grants allow it."""
    deletable_sources = await _readable_source_apps(
        current_user, client_app, session, action="delete"
    )
    repository = AppRecordRepository(session)
    record = await repository.get_for_user(user_id=current_user.id, record_id=record_id)
    if record is None or record.destination_app not in deletable_sources:
        raise HTTPException(status_code=404, detail="Record not found")
    connections = AppConnectionService(session)
    if not await connections.has_active_permission(
        current_user,
        record.destination_app,
        scope=record.destination_app,
        action="delete",
    ):
        raise HTTPException(status_code=403, detail="The source app is not authorized to delete this record")

    if record.storage_type == "object" and record.object_reference:
        try:
            await get_storage_client().delete_object(
                owner_id=str(current_user.id),
                object_id=record.object_reference,
            )
        except StorageServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The record's original object could not be deleted",
            ) from exc
    await repository.delete_links_for_record(
        user_id=current_user.id, record_id=record.id
    )
    await session.delete(record)
    await session.commit()
    return None