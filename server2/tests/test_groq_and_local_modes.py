"""Tests for Groq/external and local inference modes."""

import pytest
import torch
from fastapi.testclient import TestClient

from app.main import app


class _FakeDelta:
    def __init__(self, content: str, reasoning_content: str | None = None):
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, delta: _FakeDelta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content: str, reasoning_content: str | None = None):
        self.choices = [_FakeChoice(_FakeDelta(content, reasoning_content))]


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return [
            _FakeChunk("External ", None),
            _FakeChunk("provider reply.", None),
        ]


class _FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})


def _provision_brain(client, user_id: str, brain_id: str) -> None:
    response = client.post(
        "/v1/brains/provision",
        headers={"Authorization": "Bearer test-token"},
        json={
            "brain_id": brain_id,
            "owner_id": user_id,
            "ai_identity_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert response.status_code == 200, response.text


def test_groq_external_mode_forwards(monkeypatch, tmp_path):
    # Configure environment for external Groq mode
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("EXTERNAL_AI_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("EXTERNAL_AI_KEY", "fake-key")
    monkeypatch.setenv("EXTERNAL_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv(
        "STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'groq.db'}"
    )

    from app import storage_config, storage_db
    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle import manager

    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage_db._schema_ready_engine = None
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()

    # Mock the Groq SDK client to return a streamed reply
    fake_client = _FakeClient()
    monkeypatch.setattr("app.external_proxy._build_client", lambda: fake_client)

    client = TestClient(app)
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    brain_id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    _provision_brain(client, user_id, brain_id)

    payload = {
        "user_id": user_id,
        "query": "Reply in one short sentence explaining what you can do.",
        "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
        "model": "Qwen3.5-0.8B",
    }
    resp = client.post("/v1/brain/infer", json=payload, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["response"] == "External provider reply."
    assert data["model"] == "qwen/qwen3.8-27b"
    assert data["provider"] == "external"
    assert data["brain_id"] == brain_id
    assert data["llm_runtime_id"]
    assert data["runtime_isolation"] == "context_isolated_shared_weights"


def test_local_model_mode_with_fake_runtime(monkeypatch, tmp_path):
    # Ensure local model mode (no skip)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.setenv("BRAIN_RUNTIME_MODE", "simulated")
    monkeypatch.setenv(
        "STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    )
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)

    from app import storage_config, storage_db
    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle import manager

    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage_db._schema_ready_engine = None
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()

    client = TestClient(app)
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    brain_id = "b7d62f43-8a92-4c01-bd75-2e81ac07f963"
    _provision_brain(client, user_id, brain_id)

    payload = {
        "user_id": user_id,
        "query": "Reply in one short sentence explaining what you can do.",
        "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
        "model": "Qwen3.5-0.8B",
    }

    resp = client.post("/v1/brain/infer", json=payload, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "simulated"
    assert data["brain_id"] == brain_id
    assert data["llm_runtime_id"]
    assert data["runtime_isolation"] == "simulated"
    assert data["response"].startswith("[simulated:")
