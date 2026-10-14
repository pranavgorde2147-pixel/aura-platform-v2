"""Standalone object-storage ASGI entrypoint for Server 2.

The storage engine is part of Server 2 and is normally served together with the
brain runtime by ``app.main``. This module exposes the *same* storage endpoints
(``/v1/objects`` and ``/v1/storage/*``) without importing torch/transformers, so
the storage API can be run and integration-tested on its own and without loading
a language model.

This is not a third service: it is an alternate entrypoint for the storage
subsystem already hosted by Server 2. Production deployments still use
``app.main:app``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI

from app.api.objects import router as objects_router
from app.api.storage_health import router as storage_health_router
from app.storage_db import dispose_engine, init_models

logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Load ``server2/.env`` for local runs (real env vars always win)."""
    if os.getenv("AURA_SKIP_DOTENV", "").lower() in {"1", "true", "yes", "on"}:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


_load_env_file()

app = FastAPI(title="AURA Storage (Server 2)", version="1.0.0")

# Same paths as app.main so Server 1's StorageClient contract is identical.
app.include_router(objects_router)
app.include_router(storage_health_router, prefix="/v1/storage")


@app.on_event("startup")
async def startup_storage() -> None:
    """Create storage metadata tables; readiness reports failures."""
    try:
        await init_models()
    except Exception as exc:  # pragma: no cover - runtime guard
        logger.warning("Storage metadata init failed: %s", exc)


@app.on_event("shutdown")
async def shutdown_storage() -> None:
    await dispose_engine()
