"""Unified infrastructure health reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.infrastructure.database.health import check_database_health
from app.infrastructure.database.session import get_db
from app.infrastructure.qdrant.client import qdrant_client
from app.infrastructure.qdrant.health import check_qdrant_health
from app.infrastructure.redis.client import redis_client


async def gather_infrastructure_health() -> dict[str, Any]:
    """Collect health status across database, Redis, and Qdrant."""
    settings = get_settings()
    services: dict[str, dict[str, Any]] = {}

    try:
        async for session in get_db():
            database_connected = await check_database_health(session)
            break
        services["database"] = {"status": "connected" if database_connected else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        services["database"] = {"status": "disconnected", "error": str(exc)}

    try:
        redis = await redis_client._client.ping() if redis_client._client is not None else None
        services["redis"] = {"status": "connected" if redis else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        services["redis"] = {"status": "disconnected", "error": str(exc)}

    try:
        qdrant = qdrant_client.connect()
        services["qdrant"] = {"status": "connected" if check_qdrant_health(qdrant) else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        services["qdrant"] = {"status": "disconnected", "error": str(exc)}

    overall_status = "healthy" if all(service["status"] == "connected" for service in services.values()) else "degraded"
    return {
        "status": overall_status,
        "services": services,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
