"""Per-user Brain registry and service-auth boundary tests (no torch required)."""

from __future__ import annotations

from io import BytesIO
from uuid import UUID, uuid4

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


def _provision(client):
    brain_id, owner_id, identity_id = uuid4(), uuid4(), uuid4()
    response = _request(
        client,
        brain_id=brain_id,
        owner_id=owner_id,
        ai_identity_id=identity_id,
    )
    assert response.status_code == 200, response.text
    return {
        "brain_id": str(brain_id),
        "owner_id": str(owner_id),
        "identity_id": str(identity_id),
    }


def _auth(owner_id=None):
    headers = {"Authorization": "Bearer registry-test-token"}
    if owner_id is not None:
        headers["X-Aura-Owner-Id"] = str(owner_id)
    return headers


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
    # The simulator reports its actual isolation level rather than claiming
    # separate production model weights.
    body = first.json()
    assert body["llm_runtime_status"] == "ready_simulated"
    assert body["inference_runtime_status"] == "ready_simulated"
    assert body["runtime_isolation"] == "simulated"
    assert body["llm_runtime_id"]
    assert body["llm_runtime_id"] == repeated.json()["llm_runtime_id"]
    assert body["provisioning_status"] == "ready"


def test_runtime_capacity_defers_new_brains_and_reuses_idle_slots(
    client, monkeypatch
):
    from app.brain_manager.config import reset_brain_manager_config
    from app.brain_manager.lifecycle.manager import (
        get_runtime_manager,
        reset_runtime_manager,
    )

    monkeypatch.setenv("BRAIN_MAX_CONCURRENT_RUNTIMES", "1")
    reset_brain_manager_config()
    reset_runtime_manager()

    first = _provision(client)
    first_status = client.get(
        f"/v1/brains/{first['brain_id']}", headers=_auth()
    ).json()
    assert first_status["provisioning_status"] == "ready"
    assert first_status["llm_runtime_status"] == "ready_simulated"
    assert get_runtime_manager().active_brain_ids() == [
        UUID(first["brain_id"])
    ]

    second = _provision(client)
    second_status = client.get(
        f"/v1/brains/{second['brain_id']}", headers=_auth()
    ).json()
    assert second_status["provisioning_status"] == "ready"
    assert second_status["llm_runtime_status"] == "ready_simulated"
    manager = get_runtime_manager()
    assert manager.handle(UUID(first["brain_id"])) is None
    assert manager.handle(UUID(second["brain_id"])) is not None

    first_status = client.get(
        f"/v1/brains/{first['brain_id']}", headers=_auth()
    ).json()
    assert first_status["llm_runtime_status"] == "stopped"
    assert first_status["provisioning_status"] == "ready"

    reloaded = _request(
        client,
        brain_id=UUID(first["brain_id"]),
        owner_id=UUID(first["owner_id"]),
        ai_identity_id=UUID(first["identity_id"]),
    )
    assert reloaded.status_code == 200, reloaded.text
    assert manager.handle(UUID(first["brain_id"])) is not None
    assert manager.handle(UUID(second["brain_id"])) is None

    reset_runtime_manager()
    reset_brain_manager_config()


def test_unified_vault_idempotency_namespace_and_owner_isolation(client):
    brain = _provision(client)
    url = f"/v1/brains/{brain['brain_id']}/vault/records"
    headers = _auth(brain["owner_id"])
    payload = {
        "record_type": "diary_entry",
        "namespace": "apps/diary",
        "idempotency_key": "diary-entry-001",
        "title": "Exam week",
        "content": "I felt stressed about my exam.",
        "source_app": "diary",
        "metadata": {"is_original": True},
    }

    first = client.post(url, headers=headers, json=payload)
    repeated = client.post(url, headers=headers, json=payload)
    assert first.status_code == repeated.status_code == 201
    record_id = first.json()["record_id"]
    assert repeated.json()["record_id"] == record_id
    assert first.json()["namespace"] == "apps/diary"

    conflict = client.post(
        url, headers=headers, json={**payload, "content": "different content"}
    )
    assert conflict.status_code == 409
    bad_namespace = client.post(
        url, headers=headers, json={**payload, "idempotency_key": "bad", "namespace": "apps/study"}
    )
    assert bad_namespace.status_code == 422

    owner_b = _provision(client)
    hidden = client.get(
        f"/v1/brains/{brain['brain_id']}/vault/records/{record_id}",
        headers=_auth(owner_b["owner_id"]),
    )
    assert hidden.status_code == 404

    scoped = client.get(
        f"/v1/brains/{brain['brain_id']}/vault/records?namespace=apps%2Fdiary",
        headers=headers,
    )
    assert scoped.status_code == 200
    assert [item["record_id"] for item in scoped.json()["items"]] == [record_id]


def test_vault_versioned_edits_and_confirmed_soft_delete(client):
    brain = _provision(client)
    base = f"/v1/brains/{brain['brain_id']}/vault"
    headers = _auth(brain["owner_id"])
    created = client.post(
        f"{base}/records",
        headers=headers,
        json={
            "record_type": "note",
            "namespace": "apps/notes",
            "idempotency_key": "note-001",
            "content": "Original note",
            "source_app": "notes",
            "metadata": {"is_original": True},
        },
    )
    assert created.status_code == 201
    record_id = created.json()["record_id"]

    denied = client.put(
        f"{base}/records/{record_id}",
        headers=headers,
        json={
            "expected_version": 1,
            "content": "Changed without confirmation",
        },
    )
    assert denied.status_code == 403

    updated = client.put(
        f"{base}/records/{record_id}",
        headers=headers,
        json={
            "expected_version": 1,
            "content": "Owner-approved update",
            "confirm_original_edit": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["content"] == "Owner-approved update"
    assert updated.json()["version"] == 2

    stale = client.put(
        f"{base}/records/{record_id}",
        headers=headers,
        json={"expected_version": 1, "title": "stale"},
    )
    assert stale.status_code == 409

    no_confirmation = client.delete(
        f"{base}/records/{record_id}", headers=headers
    )
    assert no_confirmation.status_code == 409
    deleted = client.delete(
        f"{base}/records/{record_id}?confirm=true", headers=headers
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(
        f"{base}/records/{record_id}", headers=headers
    ).status_code == 404


def test_pdf_extraction_failure_is_reported_without_hiding_the_upload():
    from app.api.objects import _extract_pdf_text

    extracted, extraction_status = _extract_pdf_text(BytesIO(b"not a PDF"))

    assert extracted is None
    assert extraction_status == "failed"

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
