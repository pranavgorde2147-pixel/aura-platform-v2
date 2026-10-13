"""Liveness and readiness endpoints for Server 3."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from app.storage_config import get_settings
from app.storage_db import get_session_factory
from app.schemas.storage import HealthResponse
from app.storage.base import get_cached_object_store

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up. Backend status is reported, not enforced."""
    settings = get_settings()
    store = get_cached_object_store()
    backend_ok = store.health()
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.version,
        backend=store.name,
        backend_ok=backend_ok,
    )


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """Readiness: metadata database and object backend must both be usable."""
    settings = get_settings()
    store = get_cached_object_store()
    backend_ok = store.health()

    database_ok = True
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False

    if not (backend_ok and database_ok):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "backend_ok": backend_ok,
                "database_ok": database_ok,
            },
        )

    return HealthResponse(
        status="ready",
        service=settings.service_name,
        version=settings.version,
        backend=store.name,
        backend_ok=True,
    )
