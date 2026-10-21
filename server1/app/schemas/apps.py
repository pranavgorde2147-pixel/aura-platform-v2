"""Schemas for the AURA application registry and user app connections.

The catalog ("apps") lists every AURA application/module that exists or is
planned, and each entry defines its client type, purpose and default
permissions. User connections ("connections") are the per-user authorization
relationships, backed by the existing InstalledModule and ModulePermission
models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AppStatus = Literal["live", "planned", "module", "future"]
ClientType = Literal["web", "mobile", "desktop", "server", "internal"]
StorageAdapter = Literal["none", "relational", "object"]


class ModulePermissionSpec(BaseModel):
    """A single granted permission (scope + action)."""

    scope: str = Field(..., min_length=1, max_length=100)
    action: Literal["read", "write", "delete", "admin"] = "read"


class AppDefinition(BaseModel):
    """Registry entry for one AURA application or module.

    The capability fields describe what the application can actually *do* in
    the platform, not merely that it exists:

    ``categories``
        Semantic categories the Brain may route to this application.
    ``record_types``
        Record types the application is able to store.
    ``routable``
        Whether the Brain may deliver routed records here. An application that
        has no ingestion or storage integration stays ``routable=False`` even
        when it appears in the catalog.
    ``storage_adapter``
        Which storage backend holds this application's records.
    ``ingest_scope`` / ``ingest_action``
        Permission required to submit content *into* the Brain pipeline.
    ``write_scope`` / ``write_action``
        Permission required to deliver routed records *into* this application.
    """

    slug: str
    name: str
    description: str
    client_type: ClientType | None = None
    status: AppStatus = "live"
    platform_type: Literal["aura", "chronos", "shared"] = "aura"
    purpose: str = ""
    sends_to_aura: list[str] = Field(default_factory=list)
    needs_from_aura: list[str] = Field(default_factory=list)
    queries_brain: bool = False
    ingests: bool = False
    repo_path: str | None = None
    default_permissions: list[ModulePermissionSpec] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    record_types: list[str] = Field(default_factory=list)
    routable: bool = False
    storage_adapter: StorageAdapter = "none"
    ingest_scope: str | None = None
    ingest_action: str = "write"
    write_scope: str | None = None
    write_action: str = "write"


class ConnectionCreateRequest(BaseModel):
    """Authorize an app for the authenticated user."""

    module_slug: str = Field(..., min_length=1, max_length=100)
    permissions: list[ModulePermissionSpec] | None = None


class ConnectionUpdateRequest(BaseModel):
    """Update an app connection: activation state and/or granted permissions."""

    is_active: bool | None = None
    permissions: list[ModulePermissionSpec] | None = None


class ConnectionPermissionResponse(BaseModel):
    """A granted permission on a user's app connection."""

    id: uuid.UUID
    scope: str
    action: str

    model_config = {"from_attributes": True}


class ConnectionResponse(BaseModel):
    """A user's app connection (InstalledModule + granted permissions)."""

    id: uuid.UUID
    module_slug: str
    module_name: str
    platform_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    permissions: list[ConnectionPermissionResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ConnectionListResponse(BaseModel):
    """All of a user's app connections."""

    items: list[ConnectionResponse] = Field(default_factory=list)


class AppIngestResponse(BaseModel):
    """Result of routing one application event into the user's brain."""

    accepted: bool = True
    source_app: str
    event_type: str
    duplicate: bool = False
    partial: bool = False
    memory_id: str | None = None
    vault_entry_id: str | None = None
    # The application record that holds the full ingested content.
    record_id: str | None = None
    reason: str | None = None