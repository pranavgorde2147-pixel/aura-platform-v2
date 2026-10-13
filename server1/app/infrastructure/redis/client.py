"""Async Redis client configuration.

When ``USE_FAKE_REDIS`` is enabled (local development / tests) an in-process
``fakeredis`` instance is used so Redis is not a hard dependency. In
production this always points at the real ``REDIS_URL``.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio
from redis.asyncio import Redis

from app.core.config import get_settings


class RedisClient:
    """Thin wrapper around the async Redis client."""

    def __init__(self) -> None:
        self._client: Redis | None = None

    async def connect(self) -> Redis:
        """Create and cache an async Redis client instance."""
        if self._client is None:
            settings = get_settings()
            if settings.use_fake_redis:
                import fakeredis.aioredis

                self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
            else:
                if not settings.redis_url:
                    raise ValueError("REDIS_URL is not configured")
                self._client = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
                await self._client.ping()
        return self._client

    async def close(self) -> None:
        """Close the Redis connection if it exists."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


redis_client = RedisClient()
