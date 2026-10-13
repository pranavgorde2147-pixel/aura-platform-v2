"""Test fixtures for Server 3."""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-storage-token"


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Configure an isolated SQLite metadata DB and local object root."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_API_TOKEN", TOKEN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(data_dir / "objects"))
    monkeypatch.setenv("STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'storage.db'}")
    monkeypatch.setenv("MAX_UPLOAD_SIZE_BYTES", "1048576")
    monkeypatch.setenv("ALLOWED_CONTENT_TYPES", "")

    from app import config, database, storage

    config.get_settings.cache_clear()
    storage.reset_object_store()

    yield {"data_dir": data_dir, "config": config, "database": database, "storage": storage}


@pytest.fixture()
def client(app_env):
    """A TestClient bound to a fresh application instance."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}
