"""Business logic for storing and retrieving objects."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage_config import get_settings
from app.brain_manager.db_models import StoredObject
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.storage_db import ManagedBrain
from app.storage.base import ObjectStore, get_brain_object_store


class ObjectLimitExceeded(Exception):
    """Raised when an upload exceeds the configured size limit."""


class UnsupportedContentType(Exception):
    """Raised when an upload uses a disallowed content type."""


def object_key(
    owner_id: uuid.UUID, object_id: uuid.UUID, brain_id: uuid.UUID | None = None
) -> str:
    """Build a trusted owner/Brain-scoped storage key."""
    if brain_id is not None:
        return f"owners/{owner_id}/brains/{brain_id}/{object_id}"
    return f"owners/{owner_id}/{object_id}"


def checksum_and_size(stream: BinaryIO, chunk_size: int = 64 * 1024) -> tuple[str, int]:
    """Read ``stream`` fully, returning (sha256_hex, size_bytes)."""
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


async def store_object(
    session: AsyncSession,
    store: ObjectStore,
    *,
    owner_id: uuid.UUID,
    stream: BinaryIO,
    filename: str,
    content_type: str,
    ai_identity_id: uuid.UUID | None = None,
    source_app: str | None = None,
) -> StoredObject:
    """Validate and persist an object, returning its metadata record."""
    settings = get_settings()
    brain = None
    if ai_identity_id is not None:
        brain_result = await session.execute(
            select(ManagedBrain).where(
                ManagedBrain.owner_id == owner_id,
                ManagedBrain.ai_identity_id == ai_identity_id,
                ManagedBrain.provisioning_status == "ready",
            )
        )
        brain = brain_result.scalars().first()
        if brain is None:
            raise PermissionError("AI identity is not provisioned for this owner")
    else:
        brain_result = await session.execute(
            select(ManagedBrain).where(
                ManagedBrain.owner_id == owner_id,
                ManagedBrain.provisioning_status == "ready",
            )
        )
        brain = brain_result.scalars().first()

    if brain is None:
        raise PermissionError("A provisioned Brain is required for private object storage")
    brain_id = brain.brain_id

    if settings.allowed_content_types and content_type not in settings.allowed_content_types:
        raise UnsupportedContentType(content_type)

    checksum, size = checksum_and_size(stream)
    if size == 0:
        raise ValueError("Empty uploads are not allowed")
    if size > settings.max_upload_size_bytes:
        raise ObjectLimitExceeded(str(settings.max_upload_size_bytes))

    stream.seek(0)

    object_id = uuid.uuid4()
    key = object_key(owner_id, object_id, brain_id)
    target_store = get_brain_object_store(brain)

    expires_at = None
    if settings.default_retention_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.default_retention_days)

    record = StoredObject(
        id=object_id,
        owner_id=owner_id,
        ai_identity_id=ai_identity_id,
        # The Brain that owns the object, so the row itself records the
        # Brain-scoped namespace the blob was written to.
        brain_id=brain_id,
        object_key=key,
        filename=filename or "unnamed",
        content_type=content_type or "application/octet-stream",
        size_bytes=size,
        checksum_sha256=checksum,
        storage_backend=target_store.name,
        source_app=source_app,
        expires_at=expires_at,
    )

    target_store.save(key, stream, size)
    try:
        async with get_brain_vault_storage().session(brain) as vault_session:
            vault_session.add(record)
            await vault_session.commit()
    except Exception:
        target_store.delete(key)
        raise
    return record


async def object_store_for_record(
    session: AsyncSession, record: StoredObject, fallback: ObjectStore
) -> ObjectStore:
    """Resolve the private object location through the trusted registry row."""
    result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.brain_id == record.brain_id,
            ManagedBrain.owner_id == record.owner_id,
        )
    )
    brain = result.scalars().first()
    if brain is None:
        raise PermissionError("Object Brain is not provisioned for this owner")
    return get_brain_object_store(brain)


async def get_object(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    object_id: uuid.UUID,
) -> StoredObject | None:
    """Fetch an object's metadata from the owner's Brain vault."""
    brain_result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.owner_id == owner_id,
            ManagedBrain.provisioning_status == "ready",
        )
    )
    brain = brain_result.scalars().first()
    if brain is None:
        return None
    async with get_brain_vault_storage().session(brain) as vault_session:
        result = await vault_session.execute(
            select(StoredObject).where(
                StoredObject.id == object_id,
                StoredObject.owner_id == owner_id,
                StoredObject.brain_id == brain.brain_id,
            )
        )
        return result.scalars().first()


async def list_objects(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StoredObject], int]:
    """List object metadata from the owner's private Brain vault."""
    brain_result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.owner_id == owner_id,
            ManagedBrain.provisioning_status == "ready",
        )
    )
    brain = brain_result.scalars().first()
    if brain is None:
        return [], 0
    async with get_brain_vault_storage().session(brain) as vault_session:
        base = select(StoredObject).where(
            StoredObject.owner_id == owner_id,
            StoredObject.brain_id == brain.brain_id,
        )
        total = await vault_session.scalar(
            select(func.count()).select_from(base.subquery())
        )
        result = await vault_session.execute(
            base.order_by(StoredObject.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), int(total or 0)


async def delete_object(
    session: AsyncSession,
    store: ObjectStore,
    *,
    owner_id: uuid.UUID,
    object_id: uuid.UUID,
) -> bool:
    """Delete an object (metadata + blob), enforcing ownership."""
    record = await get_object(session, owner_id=owner_id, object_id=object_id)
    if record is None:
        return False
    target_store = await object_store_for_record(session, record, store)
    target_store.delete(record.object_key)
    brain_result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.brain_id == record.brain_id,
            ManagedBrain.owner_id == owner_id,
        )
    )
    brain = brain_result.scalars().first()
    if brain is None:
        raise PermissionError("Object Brain is not provisioned for this owner")
    async with get_brain_vault_storage().session(brain) as vault_session:
        await vault_session.execute(
            delete(StoredObject).where(
                StoredObject.id == record.id,
                StoredObject.brain_id == brain.brain_id,
                StoredObject.owner_id == owner_id,
            )
        )
        await vault_session.commit()
    return True
