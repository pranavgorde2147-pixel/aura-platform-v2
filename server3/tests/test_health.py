"""Health and authentication tests for Server 3."""

from __future__ import annotations

from uuid import uuid4


def test_liveness(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backend"] == "local"
    assert body["backend_ok"] is True


def test_readiness(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_object_endpoints_require_internal_token(client):
    owner_id = str(uuid4())
    assert client.get(f"/v1/objects?owner_id={owner_id}").status_code == 401

    response = client.post(
        "/v1/objects",
        headers={"X-Aura-Owner-Id": owner_id},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401


def test_invalid_token_rejected(client):
    response = client.get(
        f"/v1/objects?owner_id={uuid4()}",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_owner_id_required(client, auth_headers):
    response = client.get("/v1/objects", headers=auth_headers)
    assert response.status_code == 400
