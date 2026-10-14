"""File API routes.

Clients never talk to the storage API directly. They authenticate against
Server 1, which authorizes the operation and forwards it to the storage engine
using the authenticated user's id as the owner. This module is the only Server 1
code that constructs storage requests.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.storage_client import StorageServiceError, get_storage_client
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.timeline import TimelineRepository

logger = get_logger(__name__)
router = APIRouter(prefix="/files", tags=["Files"])


async def _resolve_ai_identity_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    identity = await AIIdentityRepository(session).get_by_user_id(user_id)
    return identity.id if identity else None


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Read an upload while enforcing the maximum size."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="File exceeds the upload size limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    source_app: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a file owned by the authenticated user via the storage API."""
    settings = get_settings()
    content = await _read_limited(file, settings.max_upload_size_bytes)
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty uploads are not allowed")

    identity_id = await _resolve_ai_identity_id(session, current_user.id)
    client = get_storage_client()
    try:
        record = await client.upload(
            owner_id=str(current_user.id),
            filename=file.filename or "unnamed",
            content=content,
            content_type=file.content_type or "application/octet-stream",
            ai_identity_id=str(identity_id) if identity_id else None,
            source_app=source_app,
        )
    except StorageServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    if identity_id is not None:
        try:
            timeline = TimelineRepository(session)
            await timeline.create(
                ai_identity_id=identity_id,
                event_type="file.uploaded",
                resource_type="stored_object",
                resource_reference=str(record.get("id")),
                summary=f"Uploaded {record.get('filename')}",
            )
            await timeline.commit()
        except Exception as exc:  # pragma: no cover - non-critical bookkeeping
            logger.warning("Failed to record upload timeline event: %s", exc)

    return record


@router.get("")
async def list_files(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> dict:
    """List files owned by the authenticated user."""
    client = get_storage_client()
    try:
        return await client.list_objects(owner_id=str(current_user.id), limit=limit, offset=offset)
    except StorageServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/{object_id}/metadata")
async def file_metadata(
    object_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return metadata for an owned file."""
    client = get_storage_client()
    try:
        record = await client.get_metadata(owner_id=str(current_user.id), object_id=str(object_id))
    except StorageServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return record


@router.get("/{object_id}")
async def download_file(
    object_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Download an owned file through the authorized Server 1 workflow."""
    client = get_storage_client()
    try:
        headers, iterator = await client.stream_object(owner_id=str(current_user.id), object_id=str(object_id))
    except StorageServiceError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.status_code == 404 else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return StreamingResponse(iterator, media_type=headers["Content-Type"], headers=headers)


@router.delete("/{object_id}")
async def delete_file(
    object_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Delete an owned file."""
    client = get_storage_client()
    try:
        deleted = await client.delete_object(owner_id=str(current_user.id), object_id=str(object_id))
    except StorageServiceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return {"deleted": True, "object_id": str(object_id)}
