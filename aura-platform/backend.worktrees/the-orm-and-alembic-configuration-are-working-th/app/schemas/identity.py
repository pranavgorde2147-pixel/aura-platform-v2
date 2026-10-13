"""Pydantic schemas for AIIdentity and PersonalAIBrain models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AIIdentityBase(BaseModel):
    """Base AI identity schema."""

    display_name: Optional[str] = None


class AIIdentityCreate(AIIdentityBase):
    """Schema for creating a new AI identity."""

    pass


class AIIdentityResponse(AIIdentityBase):
    """Schema for AI identity API responses."""

    id: uuid.UUID
    ai_uuid: uuid.UUID
    status: str
    is_default: bool
    last_active_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PersonalAIBrainBase(BaseModel):
    """Base personal AI brain schema."""

    brain_name: str = Field(..., min_length=1, max_length=255)
    version: str = Field(default="1.0")


class PersonalAIBrainCreate(PersonalAIBrainBase):
    """Schema for creating a new personal AI brain."""

    pass


class PersonalAIBrainResponse(PersonalAIBrainBase):
    """Schema for personal AI brain API responses."""

    id: uuid.UUID
    state: Optional[str] = None
    sync_version: int
    last_sync_at: Optional[datetime] = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BrainContextResponse(BaseModel):
    """Schema for aggregated brain context."""

    brain: dict
    memory: dict
    knowledge: dict
    recent_timeline_count: int
