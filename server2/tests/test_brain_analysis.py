"""Tests for the Server 2 structured analysis endpoint."""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    os.environ["INTERNAL_API_TOKEN"] = "test-token"
    os.environ["AURA_SKIP_MODEL_LOAD"] = "1"
    return TestClient(app)


def _analyze(client, content: str, **overrides):
    payload = {
        "user_id": str(uuid4()),
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
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    os.environ["EXTERNAL_AI_URL"] = "https://example.invalid/v1"
    os.environ["EXTERNAL_AI_KEY"] = "test-key"
    os.environ["AURA_SKIP_MODEL_LOAD"] = "1"

    response = _analyze(client, "Lecture on normalisation.")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["analysis_mode"] == "deterministic"
    assert body["provider"] == "deterministic"
    assert body["attempted_provider"] == "external"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "llm_output_unusable"
    assert body["segments"]
