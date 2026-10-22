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
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.infrastructure.storage_client import StorageServiceError, get_storage_client
from app.models.user import User
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.services.app_registry import AppConnectionService

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
    brain = (
        await PersonalAIBrainRepository(session).get_by_ai_identity_id(identity_id)
        if identity_id
        else None
    )
    if brain is None or brain.provisioning_status != "ready":
        raise HTTPException(status_code=503, detail="Personal Brain is not ready")
    if source_app and not await AppConnectionService(session).has_active_permission(
        current_user, source_app, scope="files", action="write"
    ):
        raise HTTPException(status_code=403, detail="App is not authorized to upload files")
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

    extracted_content = record.get("extracted_text")
    if (
        extracted_content is None
        and str(record.get("content_type", "")).startswith("text/")
    ):
        extracted_content = content.decode("utf-8", errors="replace")[:200000]
    try:
        vault_record = await get_brain_client().vault_request(
            user_id=str(current_user.id),
            brain_id=str(brain.id),
            method="POST",
            path="records",
            json_body={
                "record_type": "document",
                "namespace": "documents",
                "idempotency_key": f"file:{record['id']}",
                "title": record.get("filename"),
                "content": extracted_content,
                "source_app": source_app or "files",
                "source_reference": str(record["id"]),
                "object_key": record.get("object_key"),
                "metadata": {
                    "object_id": str(record["id"]),
                    "content_type": record.get("content_type"),
                    "size_bytes": record.get("size_bytes"),
                    "checksum_sha256": record.get("checksum_sha256"),
                    "extraction_status": record.get("extraction_status"),
                    "is_original": True,
                },
            },
        )
    except BrainServiceError as exc:
        try:
            await client.delete_object(
                owner_id=str(current_user.id), object_id=str(record["id"])
            )
        except StorageServiceError as cleanup_error:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Vault registration failed and the uploaded object could not be "
                    "removed; contact support with the upload ID."
                ),
            ) from cleanup_error
        raise HTTPException(
            status_code=503,
            detail="Could not register the file in the Personal Brain vault.",
        ) from exc

    record["vault_record_id"] = vault_record.get("record_id")
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
    confirm: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Delete an owned file."""
    if not confirm:
        raise HTTPException(status_code=409, detail="Explicit delete confirmation is required")
    client = get_storage_client()
    try:
        metadata = await client.get_metadata(
            owner_id=str(current_user.id), object_id=str(object_id)
        )
        if metadata is None:
            raise HTTPException(status_code=404, detail="File not found")
        brain_id = metadata.get("brain_id")
        if brain_id:
            listing = await get_brain_client().vault_request(
                user_id=str(current_user.id),
                brain_id=str(brain_id),
                method="GET",
                path="records",
                params={
                    "namespace": "documents",
                    "source_reference": str(object_id),
                    "limit": 10,
                },
            )
            for item in listing.get("items", []):
                await get_brain_client().vault_request(
                    user_id=str(current_user.id),
                    brain_id=str(brain_id),
                    method="DELETE",
                    path=f"records/{item['record_id']}",
                    params={"confirm": "true"},
                )
        deleted = await client.delete_object(owner_id=str(current_user.id), object_id=str(object_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="File not found")
    except (StorageServiceError, BrainServiceError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"deleted": True, "object_id": str(object_id)}
