"""Authentication request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """Login payload."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=255)


class RefreshRequest(BaseModel):
    """Refresh payload."""

    refresh_token: str


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 900

    model_config = {"from_attributes": True}


class TokenData(BaseModel):
    """Token payload metadata."""

    user_id: str
    token_type: str
