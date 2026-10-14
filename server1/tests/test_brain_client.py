"""Tests for the hardened Server 1 -> Server 2 Brain client.

These exercise the network boundary directly (no live Server 2 required):
forwarding the authenticated user id, request ids and model; bounded retries for
transient failures; no retries for client errors; and structured, non-leaking
error reporting.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.infrastructure import brain_client as brain_client_module
from app.infrastructure.brain_client import BrainClient, BrainServiceError


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class FakeAsyncClient:
    """Replays a scripted list of results, recording every call."""

    def __init__(self, script: list, calls: list) -> None:
        self._script = script
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self._calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get(self, url, headers=None):
        self._calls.append({"method": "GET", "url": url, "json": None, "headers": headers})
        result = self._script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def configure(monkeypatch, **env) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def install_client(monkeypatch, script: list) -> list:
    calls: list = []

    def factory(*args, **kwargs):
        return FakeAsyncClient(script, calls)

    # Patch only the name looked up in the client module; monkeypatch restores it.
    monkeypatch.setattr(brain_client_module.httpx, "AsyncClient", factory)
    return calls


def test_infer_forwards_identity_model_and_headers(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example/",
            INTERNAL_API_TOKEN="secret-token",
            AI_SERVER_MODEL="Qwen3.5-0.8B",
            AI_ACCESS_CLIENT_ID="access-id",
            AI_ACCESS_CLIENT_SECRET="access-secret",
        )
        calls = install_client(
            monkeypatch,
            [FakeResponse(200, {"response": "hi", "model": "Qwen3.5-0.8B"})],
        )
        client = BrainClient()
        user_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        result = await client.infer(user_id, "hello", {"schedule": "busy"}, request_id=request_id)

        assert result["response"] == "hi"
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://brain.example/v1/brain/infer"
        assert call["json"]["user_id"] == user_id
        assert call["json"]["request_id"] == request_id
        assert call["json"]["model"] == "Qwen3.5-0.8B"
        assert call["json"]["context"] == {"schedule": "busy"}
        assert call["headers"]["Authorization"] == "Bearer secret-token"
        assert call["headers"]["X-Aura-Owner-Id"] == user_id
        assert call["headers"]["X-Request-Id"] == request_id
        assert call["headers"]["CF-Access-Client-Id"] == "access-id"
        assert call["headers"]["CF-Access-Client-Secret"] == "access-secret"

    asyncio.run(_run())


def test_auth_failure_is_not_retried(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
            AI_MAX_RETRIES=2,
        )
        calls = install_client(monkeypatch, [FakeResponse(401, text="nope")])
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {})

        assert excinfo.value.error_code == "brain_auth_failed"
        assert excinfo.value.status_code == 401
        assert len(calls) == 1

    asyncio.run(_run())


def test_unreachable_retries_bounded_then_raises(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
            AI_MAX_RETRIES=2,
            AI_RETRY_BACKOFF_SECONDS=0,
        )
        calls = install_client(monkeypatch, [httpx.ConnectError("refused")] * 3)
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {})

        assert excinfo.value.error_code == "brain_unreachable"
        assert excinfo.value.status_code is None
        assert len(calls) == 3  # initial attempt + 2 retries, then stop

    asyncio.run(_run())


def test_read_timeout_maps_to_safe_timeout_error(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
            AI_MAX_RETRIES=2,
        )
        calls = install_client(monkeypatch, [httpx.ReadTimeout("too slow")])
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {})

        assert excinfo.value.error_code == "brain_timeout"
        assert len(calls) == 1

    asyncio.run(_run())


def test_transient_503_is_retried_then_succeeds(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
            AI_MAX_RETRIES=2,
            AI_RETRY_BACKOFF_SECONDS=0,
        )
        calls = install_client(
            monkeypatch,
            [FakeResponse(503, text="unavailable"), FakeResponse(200, {"response": "ok"})],
        )
        client = BrainClient()

        result = await client.infer(str(uuid.uuid4()), "hi", {})

        assert result["response"] == "ok"
        assert len(calls) == 2

    asyncio.run(_run())


def test_invalid_payload_rejected_without_retry(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
            AI_MAX_RETRIES=2,
        )
        calls = install_client(monkeypatch, [FakeResponse(400, text="bad model")])
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {}, model="Wrong-Model")

        assert excinfo.value.error_code == "brain_rejected"
        assert excinfo.value.status_code == 400
        assert len(calls) == 1

    asyncio.run(_run())


def test_non_json_success_body_is_reported_safely(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
        )
        install_client(monkeypatch, [FakeResponse(200, json_data=ValueError("not json"))])
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {})

        assert excinfo.value.error_code == "brain_invalid_response"

    asyncio.run(_run())


def test_errors_do_not_leak_response_body_or_network_details(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://internal-host.invalid",
            INTERNAL_API_TOKEN="tok",
        )
        install_client(
            monkeypatch,
            [FakeResponse(500, text="Traceback secret=/etc/aura internal-host.invalid")],
        )
        client = BrainClient()

        with pytest.raises(BrainServiceError) as excinfo:
            await client.infer(str(uuid.uuid4()), "hi", {})

        message = str(excinfo.value)
        assert "Traceback" not in message
        assert "secret" not in message
        assert "internal-host" not in message
        assert message == "AURA Brain rejected the request"

    asyncio.run(_run())


def test_health_check_returns_none_when_unreachable(monkeypatch):
    async def _run():
        configure(
            monkeypatch,
            AI_SERVER_URL="https://brain.example",
            INTERNAL_API_TOKEN="tok",
        )
        install_client(monkeypatch, [httpx.ConnectError("refused")])
        client = BrainClient()

        assert await client.health_check() is None

    asyncio.run(_run())


def test_settings_defaults_without_environment():
    settings = Settings(_env_file=None)
    assert settings.ai_request_timeout_seconds == 120.0
    assert settings.ai_connect_timeout_seconds == 5.0
    assert settings.ai_max_retries == 2
    assert settings.ai_retry_backoff_seconds == 0.5
    assert settings.ai_verify_tls is True


def test_settings_reads_integration_environment(monkeypatch):
    monkeypatch.setenv("AI_SERVER_URL", "https://brain.example")
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    monkeypatch.setenv("AI_VERIFY_TLS", "false")
    monkeypatch.setenv("AI_ACCESS_CLIENT_ID", "cid")
    settings = Settings(_env_file=None)
    assert settings.ai_server_url == "https://brain.example"
    assert settings.ai_max_retries == 0
    assert settings.ai_verify_tls is False
    assert settings.ai_access_client_id == "cid"
