"""StorageClient URL validation, readiness path, and safe error reporting.

No real network calls: the httpx client is replaced with a scripted fake.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import get_settings
from app.infrastructure import storage_client as storage_client_module
from app.infrastructure.storage_client import (
    StorageClient,
    StorageConfigurationError,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, script: list, calls: list) -> None:
        self._script = script
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self._calls.append({"method": "GET", "url": url, "headers": headers})
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _install(monkeypatch, script: list) -> list:
    calls: list = []
    monkeypatch.setattr(
        storage_client_module.httpx,
        "AsyncClient",
        lambda *a, **k: FakeAsyncClient(script, calls),
    )
    return calls


def test_missing_url_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "")
    get_settings.cache_clear()
    client = StorageClient()
    with pytest.raises(StorageConfigurationError):
        asyncio.run(client.health_check())


def test_url_without_scheme_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "aura-server2.onrender.com")
    get_settings.cache_clear()
    client = StorageClient()
    with pytest.raises(StorageConfigurationError) as exc:
        asyncio.run(client.health_check())
    assert "scheme" in str(exc.value)


def test_configuration_error_is_a_storage_service_error(monkeypatch):
    from app.infrastructure.storage_client import StorageServiceError

    monkeypatch.setenv("STORAGE_SERVER_URL", "")
    get_settings.cache_clear()
    assert issubclass(StorageConfigurationError, StorageServiceError)


def test_valid_url_uses_storage_readiness_path(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "https://storage.example/")
    monkeypatch.setenv("STORAGE_API_TOKEN", "test-internal-token")
    get_settings.cache_clear()
    calls = _install(monkeypatch, [FakeResponse(200, {"status": "ready", "backend": "local"})])

    client = StorageClient()
    result = asyncio.run(client.health_check())

    assert result == {"status": "ready", "backend": "local"}
    assert calls[0]["url"] == "https://storage.example/v1/storage/ready"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-internal-token"


def test_non_200_reports_not_ready_without_body(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "https://storage.example")
    get_settings.cache_clear()
    _install(monkeypatch, [FakeResponse(503)])

    result = asyncio.run(StorageClient().health_check())

    assert result == {"status": "not_ready", "http_status": 503}


def test_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "https://storage.example")
    get_settings.cache_clear()
    _install(monkeypatch, [httpx.ConnectTimeout("boom")])

    assert asyncio.run(StorageClient().health_check()) is None


def test_upload_requires_valid_url(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVER_URL", "not-a-url")
    get_settings.cache_clear()
    client = StorageClient()
    with pytest.raises(StorageConfigurationError):
        asyncio.run(
            client.upload(
                owner_id="00000000-0000-0000-0000-000000000000",
                filename="x.txt",
                content=b"x",
                content_type="text/plain",
            )
        )
