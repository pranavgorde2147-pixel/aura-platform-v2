"""Central registry for infrastructure initialization and shutdown."""

from __future__ import annotations

from typing import Any

from app.infrastructure.database.engine import engine
from app.infrastructure.database.health import check_database_health
from app.infrastructure.database.session import get_db
from app.infrastructure.qdrant.client import qdrant_client
from app.infrastructure.qdrant.health import check_qdrant_health
from app.infrastructure.redis.client import redis_client
from app.infrastructure.redis.health import check_redis_health


class InfrastructureRegistry:
    """Coordinate startup and shutdown for all infrastructure services."""

    async def initialize_all(self) -> dict[str, dict[str, Any]]:
        """Initialize the database, Redis, and Qdrant services."""
        results: dict[str, dict[str, Any]] = {}

        try:
            await redis_client.connect()
            results["redis"] = {"status": "connected"}
        except Exception as exc:  # pragma: no cover - runtime guard
            results["redis"] = {"status": "failed", "error": str(exc)}

        try:
            qdrant_client.connect()
            results["qdrant"] = {"status": "connected"}
        except Exception as exc:  # pragma: no cover - runtime guard
            results["qdrant"] = {"status": "failed", "error": str(exc)}

        try:
            async for session in get_db():
                database_connected = await check_database_health(session)
                break
            results["database"] = {"status": "connected" if database_connected else "failed"}
        except Exception as exc:  # pragma: no cover - runtime guard
            results["database"] = {"status": "failed", "error": str(exc)}

        return results

    async def shutdown_all(self) -> None:
        """Shut down Redis and close Qdrant and database resources."""
        await redis_client.close()
        qdrant_client.close()


infrastructure_registry = InfrastructureRegistry()
