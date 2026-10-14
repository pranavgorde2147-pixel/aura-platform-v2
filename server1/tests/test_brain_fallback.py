"""Tests for the Server 1 external AI failover path."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.security import hash_password
from app.infrastructure.brain_client import BrainServiceError
from app.models.user import User
from app.services import real_brain_provider
from app.services.real_brain_provider import RealPersonalBrainLLMProvider


def make_user(email: str = "fallback@example.com") -> User:
    user = User(
        email=email,
        password_hash=hash_password("password123"),
        full_name="Fallback User",
        is_active=True,
    )
    user.id = uuid.uuid4()
    return user


def test_uses_external_fallback_when_server2_unreachable(monkeypatch):
    async def _run():
        monkeypatch.setattr(real_brain_provider, "get_brain_client", lambda: SimpleNamespace(infer=AsyncMock(side_effect=RuntimeError("refused"))))
        monkeypatch.setattr(
            real_brain_provider,
            "call_external_fallback",
            AsyncMock(return_value="Fallback reply"),
        )
        monkeypatch.setenv("AI_FALLBACK_ENABLED", "true")
        monkeypatch.setenv("AI_FALLBACK_URL", "https://api.groq.com/openai/v1")
        monkeypatch.setenv("AI_FALLBACK_KEY", "key")
        from app.core.config import get_settings

        get_settings.cache_clear()

        provider = RealPersonalBrainLLMProvider()
        result = await provider.process_query(make_user(), "hello", {"schedule": "busy"})

        assert result["source"] == "external_fallback"
        assert result["answer"] == "Fallback reply"

    asyncio.run(_run())


def test_server2_test_mode_response_is_labelled(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            real_brain_provider,
            "get_brain_client",
            lambda: SimpleNamespace(
                infer=AsyncMock(
                    return_value={
                        "response": "canned",
                        "model": "Qwen3.5-0.8B",
                        "provider": "test",
                    }
                )
            ),
        )
        from app.core.config import get_settings

        get_settings.cache_clear()

        provider = RealPersonalBrainLLMProvider()
        result = await provider.process_query(make_user(), "hello", {})

        assert result["source"] == "server2_test_mode"
        assert result["provider"] == "test"
        assert result["answer"] == "canned"

    asyncio.run(_run())


def test_no_fallback_when_disabled(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            real_brain_provider,
            "get_brain_client",
            lambda: SimpleNamespace(infer=AsyncMock(side_effect=RuntimeError("refused"))),
        )
        monkeypatch.setenv("AI_FALLBACK_ENABLED", "false")
        monkeypatch.setenv("AI_FALLBACK_URL", "https://api.groq.com/openai/v1")
        monkeypatch.setenv("AI_FALLBACK_KEY", "key")
        from app.core.config import get_settings

        get_settings.cache_clear()

        provider = RealPersonalBrainLLMProvider()
        result = await provider.process_query(make_user(), "hello", {})

        assert result["source"] == "server2_error"

    asyncio.run(_run())


def test_server2_errors_are_sanitized_for_clients(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            real_brain_provider,
            "get_brain_client",
            lambda: SimpleNamespace(
                infer=AsyncMock(
                    side_effect=BrainServiceError(
                        "brain_unreachable",
                        "AURA Brain service is unreachable",
                    )
                )
            ),
        )
        monkeypatch.setenv("AI_FALLBACK_ENABLED", "false")
        from app.core.config import get_settings

        get_settings.cache_clear()

        provider = RealPersonalBrainLLMProvider()
        result = await provider.process_query(make_user(), "hello", {"schedule": "busy"})

        assert result["source"] == "server2_error"
        assert result["error_code"] == "brain_unreachable"
        # No raw exception/host detail is echoed back to the client.
        assert result["answer"] == (
            "The AURA Brain is temporarily unavailable. Please try again shortly."
        )
        assert "Traceback" not in result["answer"]

    asyncio.run(_run())


def test_external_fallback_strips_sensitive_context(monkeypatch):
    async def _run():
        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        monkeypatch.setattr(real_brain_provider.httpx, "AsyncClient", FakeClient)
        monkeypatch.setenv("AI_FALLBACK_URL", "https://api.groq.com/openai/v1")
        monkeypatch.setenv("AI_FALLBACK_KEY", "key")
        from app.core.config import get_settings

        get_settings.cache_clear()

        answer = await real_brain_provider.call_external_fallback(
            "hello",
            {"schedule": "busy", "password": "hunter2", "token": "abc"},
        )

        assert answer == "ok"
        assert captured["url"].endswith("/chat/completions")
        serialized = str(captured["json"])
        assert "hunter2" not in serialized
        assert "abc" not in serialized
        assert "busy" in serialized

    asyncio.run(_run())
