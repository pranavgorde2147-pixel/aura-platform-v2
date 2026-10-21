"""Contracts for owner-scoped canonical application records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AppRecordResponse(BaseModel):
    """Canonical record metadata returned through the authenticated vault API."""

    id: uuid.UUID
    source_app: str
    record_type: str
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    storage_type: str
    original_content_reference: str | None = None
    source_reference: str | None = None
    categories: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    access_policy: str
    processing_status: str
    version: int
    occurred_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AppRecordLinkCreateRequest(BaseModel):
    """Link one existing record to another record or a registered app."""

    related_record_id: uuid.UUID | None = None
    target_app: str | None = Field(default=None, min_length=1, max_length=100)
    relationship: str = Field(default="related", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_one_target(self) -> "AppRecordLinkCreateRequest":
        if (self.related_record_id is None) == (self.target_app is None):
            raise ValueError("Exactly one of related_record_id or target_app is required")
        return self


class AppRecordLinkResponse(BaseModel):
    """A canonical relationship or a reference to a destination app."""

    id: uuid.UUID
    record_id: uuid.UUID
    related_record_id: uuid.UUID | None = None
    target_app: str | None = None
    relationship: str
    created_at: datetime
