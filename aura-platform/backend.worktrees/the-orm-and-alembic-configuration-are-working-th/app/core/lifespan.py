"""Application startup and shutdown lifecycle hooks."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown events for the FastAPI application."""
    configure_logging()
    logger.info("Application startup initiated")
    yield
    logger.info("Application shutdown completed")
