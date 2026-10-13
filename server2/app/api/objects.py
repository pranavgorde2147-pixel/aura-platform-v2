"""Object storage API endpoints.

All endpoints require the shared internal service token and an explicit owner
id (supplied by Server 1 after it authenticates the end user). Every read,
list and delete is filtered by that owner id so an object id alone can never be
used to access another user's data.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage_db import get_db
from app.schemas.storage import DeleteResponse, ObjectListResponse, ObjectMetadata
from app.security.storage import require_internal_token
from app.services.objects import (
    ObjectLimitExceeded,
    UnsupportedContentType,
    delete_object,
    get_object,
    list_objects,
    store_object,
)
from app.storage.base import get_cached_object_store

router = APIRouter(prefix="/v1/objects", tags=["Objects"], dependencies=[Depends(require_internal_token)])


def resolve_owner_id(
    x_aura_owner_id: str | None = Header(default=None, alias="X-Aura-Owner-Id"),
    owner_id: str | None = Query(default=None),
) -> uuid.UUID:
    """Resolve and validate the owning user id."""
    raw = x_aura_owner_id or owner_id
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="owner id is required (X-Aura-Owner-Id header or owner_id query param)",
        )
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner id must be a UUID") from exc


@router.post("", response_model=ObjectMetadata, status_code=status.HTTP_201_CREATED)
async def upload_object(
    file: UploadFile = File(...),
    owner_id: uuid.UUID = Depends(resolve_owner_id),
    ai_identity_id: str | None = Form(default=None),
    source_app: str | None = Form(default=None),
    session: AsyncSession = Depends(get_db),
) -> ObjectMetadata:
    """Upload a new object owned by ``owner_id``."""
    parsed_identity: uuid.UUID | None = None
    if ai_identity_id:
        try:
            parsed_identity = uuid.UUID(ai_identity_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ai_identity_id must be a UUID") from exc

    store = get_cached_object_store()
    try:
        record = await store_object(
            session,
            store,
            owner_id=owner_id,
            stream=file.file,
            filename=file.filename or "unnamed",
            content_type=file.content_type or "application/octet-stream",
            ai_identity_id=parsed_identity,
            source_app=source_app,
        )
    except ObjectLimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File exceeds the upload size limit") from exc
    except UnsupportedContentType as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Content type not allowed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return ObjectMetadata.model_validate(record)


@router.get("", response_model=ObjectListResponse)
async def list_owned_objects(
    owner_id: uuid.UUID = Depends(resolve_owner_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> ObjectListResponse:
    """List objects owned by ``owner_id``."""
    items, total = await list_objects(session, owner_id=owner_id, limit=limit, offset=offset)
    return ObjectListResponse(
        items=[ObjectMetadata.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{object_id}/metadata", response_model=ObjectMetadata)
async def object_metadata(
    object_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(resolve_owner_id),
    session: AsyncSession = Depends(get_db),
) -> ObjectMetadata:
    """Return metadata for an owned object."""
    record = await get_object(session, owner_id=owner_id, object_id=object_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return ObjectMetadata.model_validate(record)


@router.get("/{object_id}")
async def download_object(
    object_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(resolve_owner_id),
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream an owned object's bytes."""
    record = await get_object(session, owner_id=owner_id, object_id=object_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    store = get_cached_object_store()
    if not store.exists(record.object_key):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Object blob is missing")

    return StreamingResponse(
        store.open_stream(record.object_key),
        media_type=record.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            "X-Object-Checksum-Sha256": record.checksum_sha256,
        },
    )


@router.delete("/{object_id}", response_model=DeleteResponse)
async def remove_object(
    object_id: uuid.UUID,
    owner_id: uuid.UUID = Depends(resolve_owner_id),
    session: AsyncSession = Depends(get_db),
) -> DeleteResponse:
    """Delete an owned object and its metadata."""
    store = get_cached_object_store()
    deleted = await delete_object(session, store, owner_id=owner_id, object_id=object_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    return DeleteResponse(deleted=True, object_id=object_id)
