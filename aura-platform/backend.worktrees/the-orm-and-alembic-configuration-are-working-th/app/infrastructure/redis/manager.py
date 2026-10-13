"""Redis connection manager for startup and shutdown lifecycle."""

from __future__ import annotations

from redis.asyncio import Redis

from app.infrastructure.redis.client import redis_client


class RedisManager:
    """Manage Redis connection lifecycle for application startup and shutdown."""

    async def connect(self) -> Redis:
        """Connect to Redis and return the client."""
        return await redis_client.connect()

    async def close(self) -> None:
        """Close the Redis connection."""
        await redis_client.close()


redis_manager = RedisManager()
