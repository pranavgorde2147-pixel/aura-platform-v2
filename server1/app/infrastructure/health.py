"""Unified infrastructure and dependency health reporting.

Liveness ("is the process up?") is intentionally separated from readiness
("can this instance serve traffic?"). Readiness requires the core datastores
(PostgreSQL, Redis, Qdrant). Server 2 (AI) and Server 3 (storage) are reported
as dependencies; their absence degrades the platform but does not make core
identity/auth operations unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.infrastructure.brain_client import get_brain_client
from app.infrastructure.database.health import check_database_health
from app.infrastructure.database.session import get_db
from app.infrastructure.qdrant.client import qdrant_client
from app.infrastructure.qdrant.health import check_qdrant_health
from app.infrastructure.redis.client import redis_client
from app.infrastructure.storage_client import get_storage_client


async def _check_database() -> dict[str, Any]:
    try:
        async for session in get_db():
            connected = await check_database_health(session)
            break
        else:  # pragma: no cover - generator always yields
            connected = False
        return {"status": "connected" if connected else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        return {"status": "disconnected", "error": type(exc).__name__}


async def _check_redis() -> dict[str, Any]:
    try:
        client = await redis_client.connect()
        pong = await client.ping()
        return {"status": "connected" if pong else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        return {"status": "disconnected", "error": type(exc).__name__}


async def _check_qdrant() -> dict[str, Any]:
    try:
        client = qdrant_client.connect()
        ok = await asyncio.to_thread(check_qdrant_health, client)
        return {"status": "connected" if ok else "disconnected"}
    except Exception as exc:  # pragma: no cover - runtime guard
        return {"status": "disconnected", "error": type(exc).__name__}


async def _check_ai_server() -> dict[str, Any]:
    try:
        result = await get_brain_client().health_check()
    except Exception as exc:  # pragma: no cover - runtime guard
        return {"status": "unavailable", "error": type(exc).__name__}
    if result is None:
        return {"status": "unavailable"}
    return {"status": "connected", "model": result.get("model"), "provider": result.get("provider")}


async def _check_storage_server() -> dict[str, Any]:
    try:
        result = await get_storage_client().health_check()
    except Exception as exc:  # pragma: no cover - runtime guard
        return {"status": "unavailable", "error": type(exc).__name__}
    if result is None:
        return {"status": "unavailable"}
    return {"status": "connected", "backend": result.get("backend")}


async def gather_infrastructure_health() -> dict[str, Any]:
    """Collect health status across all core and dependent services."""
    settings = get_settings()

    database, redis, qdrant, ai_server, storage = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_qdrant(),
        _check_ai_server(),
        _check_storage_server(),
    )

    services = {
        "database": database,
        "redis": redis,
        "qdrant": qdrant,
        "ai_server": ai_server,
        "storage_server": storage,
    }

    core_ready = all(services[name]["status"] == "connected" for name in ("database", "redis", "qdrant"))
    dependencies_ok = all(
        services[name]["status"] == "connected" for name in ("ai_server", "storage_server")
    )

    if core_ready and dependencies_ok:
        overall = "healthy"
    elif core_ready:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return {
        "status": overall,
        "core_ready": core_ready,
        "services": services,
        "version": settings.app_version,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
