"""Health-check endpoints for the backend foundation."""

from fastapi import APIRouter, HTTPException, status

from app.infrastructure.health import gather_infrastructure_health

router = APIRouter()


@router.get("/health/live", tags=["Health"])
async def liveness_check() -> dict[str, str]:
    """Liveness probe: the process is running."""
    return {"status": "alive"}


@router.get("/health", tags=["Health"])
async def health_check() -> dict[str, object]:
    """Unified liveness + dependency health status (always 200)."""
    return await gather_infrastructure_health()


@router.get("/ready", tags=["Health"])
async def readiness_check() -> dict[str, object]:
    """Readiness probe: core datastores must be reachable."""
    health = await gather_infrastructure_health()
    if not health.get("core_ready"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health)
    return health
