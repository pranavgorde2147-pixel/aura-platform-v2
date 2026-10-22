"""Per-user Brain registry and service-auth boundary tests (no torch required)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "brain-registry"
    data_dir.mkdir()
    monkeypatch.setenv("INTERNAL_API_TOKEN", "registry-test-token")
    monkeypatch.setenv("STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'registry.db'}")
    monkeypatch.setenv("STORAGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)

    from app import storage_config, storage_db
    from app.storage_app import app

    from app.storage.base import reset_object_store

    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    reset_object_store()
    with TestClient(app) as test_client:
        yield test_client
    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    reset_object_store()


def _request(client, *, brain_id, owner_id, ai_identity_id):
    return client.post(
        "/v1/brains/provision",
        headers={"Authorization": "Bearer registry-test-token"},
        json={
            "brain_id": str(brain_id),
            "owner_id": str(owner_id),
            "ai_identity_id": str(ai_identity_id),
            "model_id": "Qwen3.5-0.8B",
            "max_concurrent_requests": 1,
        },
    )


def test_brain_provisioning_is_idempotent_and_owner_scoped(client):
    brain_id, owner_id, identity_id = uuid4(), uuid4(), uuid4()
    first = _request(
        client, brain_id=brain_id, owner_id=owner_id, ai_identity_id=identity_id
    )
    repeated = _request(
        client, brain_id=brain_id, owner_id=owner_id, ai_identity_id=identity_id
    )

    assert first.status_code == repeated.status_code == 200
    assert first.json()["brain_id"] == repeated.json()["brain_id"] == str(brain_id)
    assert first.json()["storage_resource_reference"].endswith(str(brain_id))
    assert "token" not in str(first.json()).lower()

    # Every Brain gets its *own* runtime. In test mode the runtime is the
    # deterministic simulator, and it is labelled as such - it is never
    # presented as a shared production runtime.
    body = first.json()
    assert body["llm_runtime_status"] == "ready_simulated"
    assert body["inference_runtime_status"] == "ready_simulated"
    assert body["runtime_isolation"] == "simulated"
    assert body["llm_runtime_id"]
    assert body["llm_runtime_id"] == repeated.json()["llm_runtime_id"]
    assert body["provisioning_status"] == "ready"


def test_each_brain_gets_its_own_runtime(client):
    """Two Brains never share a runtime."""
    first_brain, first_owner, first_identity = uuid4(), uuid4(), uuid4()
    second_brain, second_owner, second_identity = uuid4(), uuid4(), uuid4()

    first = _request(
        client,
        brain_id=first_brain,
        owner_id=first_owner,
        ai_identity_id=first_identity,
    )
    second = _request(
        client,
        brain_id=second_brain,
        owner_id=second_owner,
        ai_identity_id=second_identity,
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["llm_runtime_id"] != second.json()["llm_runtime_id"]


def test_brain_registry_rejects_rebinding_owner_or_identity(client):
    owner_id, identity_id = uuid4(), uuid4()
    assert _request(
        client, brain_id=uuid4(), owner_id=owner_id, ai_identity_id=identity_id
    ).status_code == 200
    conflict = _request(
        client, brain_id=uuid4(), owner_id=owner_id, ai_identity_id=uuid4()
    )
    assert conflict.status_code == 409


def test_brain_registry_requires_internal_service_authentication(client):
    response = client.post(
        "/v1/brains/provision",
        json={
            "brain_id": str(uuid4()),
            "owner_id": str(uuid4()),
            "ai_identity_id": str(uuid4()),
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert response.status_code == 401


def test_brain_registry_rejects_unavailable_model_configuration(client):
    response = client.post(
        "/v1/brains/provision",
        headers={"Authorization": "Bearer registry-test-token"},
        json={
            "brain_id": str(uuid4()),
            "owner_id": str(uuid4()),
            "ai_identity_id": str(uuid4()),
            "model_id": "unavailable-model",
        },
    )
    assert response.status_code == 409


def test_brain_objects_are_scoped_to_registered_identity(client):
    brain_id, owner_id, identity_id = uuid4(), uuid4(), uuid4()
    assert _request(
        client, brain_id=brain_id, owner_id=owner_id, ai_identity_id=identity_id
    ).status_code == 200
    headers = {
        "Authorization": "Bearer registry-test-token",
        "X-Aura-Owner-Id": str(owner_id),
    }
    uploaded = client.post(
        "/v1/objects",
        headers=headers,
        data={"ai_identity_id": str(identity_id)},
        files={"file": ("private.txt", b"private brain data", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text

    unregistered = client.post(
        "/v1/objects",
        headers=headers,
        data={"ai_identity_id": str(uuid4())},
        files={"file": ("private.txt", b"private brain data", "text/plain")},
    )
    assert unregistered.status_code == 403

    from app.services.objects import object_key

    assert object_key(owner_id, uuid4(), brain_id).startswith(
        f"owners/{owner_id}/brains/{brain_id}/"
    )
