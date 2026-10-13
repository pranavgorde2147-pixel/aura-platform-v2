"""Redis health-check helpers."""

from __future__ import annotations

from redis.asyncio import Redis


async def check_redis_health(client: Redis) -> bool:
    """Verify connectivity to Redis by issuing a ping."""
    try:
        response = await client.ping()
        return bool(response)
    except Exception:
        return False
