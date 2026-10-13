"""Health-check endpoints for the backend foundation."""

from fastapi import APIRouter, HTTPException, status

from app.infrastructure.health import gather_infrastructure_health

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health_check() -> dict[str, object]:
    """Return the unified liveness and infrastructure health status."""
    return await gather_infrastructure_health()


@router.get("/ready", tags=["Health"])
async def readiness_check() -> dict[str, object]:
    """Return readiness status for all infrastructure dependencies."""
    health = await gather_infrastructure_health()
    if health["status"] != "healthy":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=health)
    return health
