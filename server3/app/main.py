"""AURA Server 3 - Storage Engine entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.config import get_settings
from app.database import dispose_engine, init_models


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise metadata storage on startup and dispose on shutdown."""
    await init_models()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Create and configure the storage engine application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        lifespan=lifespan,
        description="AURA object storage service. Internal, token-protected.",
    )
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": settings.service_name, "status": "ok"}

    return app


app = create_app()
