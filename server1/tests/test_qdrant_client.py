"""Qdrant client configuration: API key propagation and URL validation.

These tests never touch a real Qdrant instance; they assert that the client is
constructed with the configured credentials and a normalized endpoint.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.infrastructure.qdrant import client as qdrant_client_module
from app.infrastructure.qdrant.client import QdrantClientWrapper, normalize_qdrant_url


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "https://cluster.example.cloud.qdrant.io:6333/")
    monkeypatch.setenv("QDRANT_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.delenv("QDRANT_LOCAL_PATH", raising=False)
    get_settings.cache_clear()


def test_api_key_and_normalized_url_are_passed_to_client(monkeypatch, configured_env):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):  # pragma: no cover - best effort
            pass

    monkeypatch.setattr(qdrant_client_module, "QdrantClient", FakeClient)

    QdrantClientWrapper().connect()

    assert captured["url"] == "https://cluster.example.cloud.qdrant.io:6333"
    assert captured["api_key"] == "test-key-not-a-real-secret"
    assert "timeout" in captured


def test_empty_api_key_is_passed_as_none(monkeypatch, configured_env):
    monkeypatch.setenv("QDRANT_API_KEY", "")
    get_settings.cache_clear()
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(qdrant_client_module, "QdrantClient", FakeClient)
    QdrantClientWrapper().connect()

    assert captured["api_key"] is None


def test_missing_scheme_is_rejected():
    with pytest.raises(ValueError):
        normalize_qdrant_url("cluster.example.cloud.qdrant.io:6333")


def test_empty_url_is_rejected():
    with pytest.raises(ValueError):
        normalize_qdrant_url("   ")


def test_local_path_mode_does_not_require_api_key(monkeypatch):
    monkeypatch.setenv("QDRANT_LOCAL_PATH", "/tmp/qdrant-test")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    get_settings.cache_clear()
    captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(qdrant_client_module, "QdrantClient", FakeClient)
    QdrantClientWrapper().connect()

    assert captured == {"path": "/tmp/qdrant-test"}
