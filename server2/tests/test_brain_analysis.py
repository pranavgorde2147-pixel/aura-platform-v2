"""Tests for the Server 2 structured analysis endpoint."""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)
    monkeypatch.delenv("EXTERNAL_MODEL", raising=False)
    monkeypatch.setenv(
        "STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}"
    )
    os.environ["INTERNAL_API_TOKEN"] = "test-token"
    os.environ["AURA_SKIP_MODEL_LOAD"] = "1"

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
    with TestClient(app) as test_client:
        yield test_client
    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage_db._schema_ready_engine = None
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()


def _provision(client, user_id: str) -> str:
    """Register a Brain for the owner; analysis is Brain-scoped."""
    brain_id = uuid4()
    response = client.post(
        "/v1/brains/provision",
        headers={"Authorization": "Bearer test-token"},
        json={
            "brain_id": str(brain_id),
            "owner_id": str(user_id),
            "ai_identity_id": str(uuid4()),
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert response.status_code == 200, response.text
    return str(brain_id)


def _analyze(client, content: str, **overrides):
    user_id = str(uuid4())
    _provision(client, user_id)
    payload = {
        "user_id": user_id,
        "content": content,
        "model": "Qwen3.5-0.8B",
        "request_id": str(uuid4()),
        "context": {},
    }
    payload.update(overrides)
    return client.post(
        "/v1/brain/analyze",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )


def test_analysis_requires_the_internal_token(client):
    response = client.post(
        "/v1/brain/analyze",
        json={"user_id": str(uuid4()), "content": "hello"},
    )
    assert response.status_code == 401


def test_analysis_rejects_an_invalid_user_id(client):
    response = _analyze(client, "hello", user_id="not-a-uuid")
    assert response.status_code == 400


def test_analysis_rejects_empty_content(client):
    response = _analyze(client, "   ")
    assert response.status_code == 400


def test_analysis_response_is_labelled_as_deterministic_without_a_model(client):
    response = _analyze(
        client,
        "Lecture on normalisation. The submission deadline is on Friday.",
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Without a model the analyser is deterministic and says so: heuristic
    # confidences are never presented as calibrated model scores.
    assert body["analysis_mode"] == "deterministic"
    assert body["confidence_kind"] == "heuristic"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "model_load_skipped"
    assert body["segments"]

    for segment in body["segments"]:
        assert segment["text"].strip()
        assert 0.0 <= segment["confidence"] <= 1.0
        assert segment["category"]
        assert segment["record_type"]


def test_analysis_splits_independent_statements(client):
    response = _analyze(
        client,
        "Lecture on normalisation. The assignment deadline is on Friday.",
    )
    assert response.status_code == 200, response.text
    categories = {segment["category"] for segment in response.json()["segments"]}
    assert "lecture" in categories
    assert "deadline" in categories


def test_analysis_keeps_segments_indexed_and_bounded(client):
    response = _analyze(client, "Lecture on normalisation.", max_segments=1)
    assert response.status_code == 200, response.text
    segments = response.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["index"] == 0


def test_unusable_model_output_names_the_producer_of_the_labels(client, monkeypatch):
    """A heuristic answer is never presented as a model answer.

    When the model is tried and its answer cannot be parsed, the labels come
    from the deterministic analyser. The response must say so in the provider
    field, and name the attempted provider separately.
    """
    monkeypatch.setenv("EXTERNAL_AI_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EXTERNAL_AI_KEY", "test-key")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.setenv("BRAIN_RUNTIME_MODE", "external")
    os.environ["EXTERNAL_AI_URL"] = "https://example.invalid/v1"
    os.environ["EXTERNAL_AI_KEY"] = "test-key"
    os.environ["AURA_SKIP_MODEL_LOAD"] = "0"
    os.environ["BRAIN_RUNTIME_MODE"] = "external"
    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle import manager

    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()

    response = _analyze(client, "Lecture on normalisation.")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["analysis_mode"] == "deterministic"
    assert body["provider"] == "deterministic"
    assert body["attempted_provider"] == "external"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "llm_output_unusable"
    assert body["segments"]
