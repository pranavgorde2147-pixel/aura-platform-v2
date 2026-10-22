"""Pydantic schemas for the Server 2 storage API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ObjectMetadata(BaseModel):
    """Metadata returned for a stored object."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    ai_identity_id: UUID | None = None
    #: The Brain that owns the object. Objects uploaded for a Brain live under
    #: that Brain's own storage prefix.
    brain_id: UUID | None = None
    #: The trusted, owner- and Brain-scoped key inside the object store.
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    storage_backend: str
    source_app: str | None = None
    created_at: datetime
    expires_at: datetime | None = None


class ObjectListResponse(BaseModel):
    """Paginated list of objects owned by a user."""

    items: list[ObjectMetadata]
    total: int
    limit: int
    offset: int


class DeleteResponse(BaseModel):
    """Result of a delete operation."""

    deleted: bool
    object_id: UUID


class HealthResponse(BaseModel):
    """Health-check response."""

    status: str
    service: str
    version: str
    backend: str
    backend_ok: bool
