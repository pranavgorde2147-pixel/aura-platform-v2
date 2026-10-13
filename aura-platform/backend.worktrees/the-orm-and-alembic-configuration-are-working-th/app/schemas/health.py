"""Health-related response schemas."""

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response model for the health endpoint."""

    status: str = Field(..., example="healthy")
    service: str = Field(..., example="AURA Backend")
    version: str = Field(..., example="1.0.0")
