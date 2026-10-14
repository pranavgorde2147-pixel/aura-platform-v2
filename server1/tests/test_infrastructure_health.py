"""Infrastructure health aggregation and readiness semantics.

Core readiness = PostgreSQL + Redis + Qdrant. AI inference and the storage API
are dependencies: their absence yields ``degraded`` (core still ready), never
``unhealthy``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure import health as health_module
from app.infrastructure.qdrant.health import QdrantProbe
from app.infrastructure.storage_client import StorageConfigurationError


def _patch_checks(monkeypatch, **results):
    for name in (
        "_check_database",
        "_check_redis",
        "_check_qdrant",
        "_check_ai_server",
        "_check_storage_server",
    ):
        value = results.get(name)
        async def factory(_value=value):
            return _value
        monkeypatch.setattr(health_module, name, factory)


def test_all_core_up_and_dependencies_up_is_healthy(monkeypatch):
    _patch_checks(
        monkeypatch,
        _check_database={"status": "connected"},
        _check_redis={"status": "connected"},
        _check_qdrant={"status": "connected"},
        _check_ai_server={"status": "connected"},
        _check_storage_server={"status": "connected"},
    )
    result = asyncio.run(health_module.gather_infrastructure_health())
    assert result["status"] == "healthy"
    assert result["core_ready"] is True


def test_core_up_but_dependencies_down_is_degraded(monkeypatch):
    _patch_checks(
        monkeypatch,
        _check_database={"status": "connected"},
        _check_redis={"status": "connected"},
        _check_qdrant={"status": "connected"},
        _check_ai_server={"status": "unavailable"},
        _check_storage_server={"status": "unavailable", "reason": "configuration_error"},
    )
    result = asyncio.run(health_module.gather_infrastructure_health())
    assert result["status"] == "degraded"
    assert result["core_ready"] is True


def test_qdrant_down_makes_core_not_ready(monkeypatch):
    _patch_checks(
        monkeypatch,
        _check_database={"status": "connected"},
        _check_redis={"status": "connected"},
        _check_qdrant={"status": "disconnected", "reason": "forbidden"},
        _check_ai_server={"status": "connected"},
        _check_storage_server={"status": "connected"},
    )
    result = asyncio.run(health_module.gather_infrastructure_health())
    assert result["status"] == "unhealthy"
    assert result["core_ready"] is False
    assert result["services"]["qdrant"]["reason"] == "forbidden"


def test_storage_configuration_error_is_reported(monkeypatch):
    class BrokenClient:
        async def health_check(self):
            raise StorageConfigurationError("STORAGE_SERVER_URL is not configured")

    monkeypatch.setattr(health_module, "get_storage_client", lambda: BrokenClient())
    result = asyncio.run(health_module._check_storage_server())
    assert result["status"] == "unavailable"
    assert result["reason"] == "configuration_error"


def test_storage_not_ready_is_not_reported_as_connected(monkeypatch):
    class NotReadyClient:
        async def health_check(self):
            return {"status": "not_ready", "http_status": 503}

    monkeypatch.setattr(health_module, "get_storage_client", lambda: NotReadyClient())
    result = asyncio.run(health_module._check_storage_server())
    assert result["status"] == "unavailable"
    assert result["http_status"] == 503


def test_qdrant_configuration_error_is_reported(monkeypatch):
    def broken_connect():
        raise ValueError("QDRANT_URL must include an explicit scheme")

    monkeypatch.setattr(health_module.qdrant_client, "connect", broken_connect)
    result = asyncio.run(health_module._check_qdrant())
    assert result == {"status": "disconnected", "reason": "configuration_error"}


def test_qdrant_probe_failure_reports_reason(monkeypatch):
    monkeypatch.setattr(health_module.qdrant_client, "connect", lambda: object())
    monkeypatch.setattr(
        health_module,
        "probe_qdrant",
        lambda client: QdrantProbe(ok=False, reason="forbidden"),
    )
    result = asyncio.run(health_module._check_qdrant())
    assert result == {"status": "disconnected", "reason": "forbidden"}
