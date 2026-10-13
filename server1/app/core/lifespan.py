"""Application startup and shutdown lifecycle hooks."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.core.logging import configure_logging, get_logger
from app.infrastructure.redis.client import redis_client

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown events for the FastAPI application."""
    configure_logging()
    logger.info("Application startup initiated")
    
    # Connect to Redis during startup
    try:
        await redis_client.connect()
        logger.info("Redis connected successfully")
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}")
    
    yield
    
    # Disconnect from Redis during shutdown
    try:
        await redis_client.close()
        logger.info("Redis disconnected")
    except Exception as e:
        logger.warning(f"Failed to disconnect from Redis: {e}")
    
    logger.info("Application shutdown completed")
