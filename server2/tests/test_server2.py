from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.main import _build_messages


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)
    monkeypatch.delenv("EXTERNAL_MODEL", raising=False)
    monkeypatch.setenv("STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'brain-test.db'}")
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


def _provision(client, user_id: str) -> dict:
    response = client.post(
        "/v1/brains/provision",
        headers={"Authorization": "Bearer test-token"},
        json={
            "brain_id": str(uuid4()),
            "owner_id": user_id,
            "ai_identity_id": str(uuid4()),
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model"] == "Qwen3.5-0.8B"


def test_missing_auth(client):
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": str(uuid4()),
            "query": "Should I exercise?",
            "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "Qwen3.5-0.8B",
        },
    )
    assert response.status_code == 401


def test_invalid_auth(client):
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": str(uuid4()),
            "query": "Should I exercise?",
            "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "Qwen3.5-0.8B",
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_invalid_request_is_rejected(client):
    response = client.post(
        "/v1/brain/infer",
        json={"user_id": "not-uuid", "query": "", "model": "Qwen3.5-0.8B"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code in {400, 422}


def test_wrong_model_name_rejected(client):
    user_id = str(uuid4())
    _provision(client, user_id)
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": user_id,
            "query": "Tell me a short answer.",
            "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "Mistral-7B",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code in {400, 422}


def test_response_schema(client):
    user_id = str(uuid4())
    _provision(client, user_id)
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": user_id,
            "query": "Give a short answer in one sentence.",
            "context": {"user_level": "general", "relevant_context": {"topic": "fitness"}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "Qwen3.5-0.8B",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "Qwen3.5-0.8B"
    assert payload["status"] == "ok"
    assert isinstance(payload["response"], str)
    assert isinstance(payload["tokens_used"], int)
    assert isinstance(payload["latency_ms"], int)


def test_user_isolation(client):
    user_id = str(uuid4())
    _provision(client, user_id)
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": user_id,
            "query": "What should I do tonight?",
            "context": {"user_level": "general", "relevant_context": {"user": user_id}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "Qwen3.5-0.8B",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id


def test_exact_model_name_is_required(client):
    user_id = str(uuid4())
    _provision(client, user_id)
    response = client.post(
        "/v1/brain/infer",
        json={
            "user_id": user_id,
            "query": "Say hello.",
            "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
            "request_id": str(uuid4()),
            "model": "qwen3.5-4b",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code in {400, 422}


def test_inference_prompt_includes_authorized_cross_app_records_and_memories():
    system, query = _build_messages(
        "What is my pet name?",
        {
            "authorized_memories": [
                {
                    "id": "memory-1",
                    "kind": "interpretation",
                    "source": "app",
                    "source_app": "diary",
                    "content": "My pet name is Sai.",
                }
            ],
            "authorized_records": [
                {
                    "record_id": "record-1",
                    "source_app": "diary",
                    "record_type": "diary_entry",
                    "source_reference": "diary-entry-42",
                    "content": "My pet name is Sai.",
                }
            ],
        },
    )

    assert query == "What is my pet name?"
    assert "Retrieved user memories" in system
    assert '"source_app": "diary"' in system
    assert '"kind": "interpretation"' in system
    assert "Retrieved canonical user records" in system
    assert '"record_id": "record-1"' in system
    assert "My pet name is Sai." in system
