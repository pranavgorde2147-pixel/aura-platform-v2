"""Object storage tests for Server 2 (formerly the standalone storage service).

Covers upload/download/list/delete lifecycles, per-owner isolation, size and
content-type limits, and persistence across an engine restart. These prove the
same StorageClient contract Server 1 relies on continues to work now that the
storage engine lives inside Server 2.
"""

from __future__ import annotations

import os
import uuid
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-storage-token"


@pytest.fixture()
def storage_env(tmp_path, monkeypatch):
    """Isolated SQLite metadata DB + local object root for storage tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_API_TOKEN", TOKEN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(data_dir / "objects"))
    monkeypatch.setenv("STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'storage.db'}")
    monkeypatch.setenv("MAX_UPLOAD_SIZE_BYTES", "1048576")
    monkeypatch.setenv("ALLOWED_CONTENT_TYPES", "")

    from app import storage_config, storage_db, storage

    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage.reset_object_store()

    yield {"data_dir": data_dir, "storage_config": storage_config, "storage_db": storage_db, "storage": storage}


@pytest.fixture()
def client(storage_env):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _upload(client, headers, owner_id, content=b"hello world", filename="note.txt", content_type="text/plain"):
    parsed_owner = uuid.UUID(owner_id)
    brain_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aura-test-brain:{parsed_owner}")
    identity_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aura-test-identity:{parsed_owner}")
    provision = client.post(
        "/v1/brains/provision",
        headers=headers,
        json={
            "brain_id": str(brain_id),
            "owner_id": str(parsed_owner),
            "ai_identity_id": str(identity_id),
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert provision.status_code == 200, provision.text
    return client.post(
        "/v1/objects",
        headers={**headers, "X-Aura-Owner-Id": owner_id},
        files={"file": (filename, content, content_type)},
        data={"source_app": "test", "ai_identity_id": str(identity_id)},
    )


def test_upload_download_metadata_list_delete(client, auth_headers):
    owner_id = str(uuid4())
    upload = _upload(client, auth_headers, owner_id)
    assert upload.status_code == 201, upload.text
    record = upload.json()
    object_id = record["id"]
    assert record["owner_id"] == owner_id
    assert record["size_bytes"] == len(b"hello world")
    assert record["content_type"] == "text/plain"

    meta = client.get(
        f"/v1/objects/{object_id}/metadata",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
    )
    assert meta.status_code == 200
    assert meta.json()["checksum_sha256"] == record["checksum_sha256"]

    download = client.get(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
    )
    assert download.status_code == 200
    assert download.content == b"hello world"
    assert download.headers["x-object-checksum-sha256"] == record["checksum_sha256"]

    listing = client.get(f"/v1/objects?owner_id={owner_id}", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    deletion = client.delete(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
    )
    assert deletion.status_code == 200
    assert deletion.json()["deleted"] is True

    after = client.get(
        f"/v1/objects/{object_id}/metadata",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
    )
    assert after.status_code == 404


def test_cross_user_isolation(client, auth_headers):
    owner_a = str(uuid4())
    owner_b = str(uuid4())
    upload = _upload(client, auth_headers, owner_a, content=b"secret-a")
    assert upload.status_code == 201
    object_id = upload.json()["id"]

    assert client.get(
        f"/v1/objects/{object_id}/metadata",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_b},
    ).status_code == 404
    assert client.get(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_b},
    ).status_code == 404
    assert client.delete(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_b},
    ).status_code == 404

    listing_b = client.get(f"/v1/objects?owner_id={owner_b}", headers=auth_headers)
    assert listing_b.json()["total"] == 0

    assert client.get(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_a},
    ).status_code == 200


def test_missing_or_invalid_token(client):
    owner_id = str(uuid4())
    no_auth = client.post(
        "/v1/objects",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={},
    )
    assert no_auth.status_code == 401

    bad_auth = client.post(
        "/v1/objects",
        headers={"Authorization": "Bearer nope"},
        files={"file": ("a.txt", b"x", "text/plain")},
        data={},
    )
    assert bad_auth.status_code == 401

    assert client.get(f"/v1/objects?owner_id={owner_id}", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_size_limit_enforced(client, auth_headers):
    owner_id = str(uuid4())
    too_big = b"x" * (1048576 + 10)
    response = _upload(client, auth_headers, owner_id, content=too_big, filename="big.bin", content_type="application/octet-stream")
    assert response.status_code == 413


def test_content_type_allow_list(client, auth_headers, storage_env, monkeypatch):
    owner_id = str(uuid4())
    monkeypatch.setenv("ALLOWED_CONTENT_TYPES", "text/plain")
    storage_env["storage_config"].get_settings.cache_clear()

    denied = _upload(client, auth_headers, owner_id, content=b"x", filename="a.exe", content_type="application/x-msdownload")
    assert denied.status_code == 415


def test_empty_upload_rejected(client, auth_headers):
    owner_id = str(uuid4())
    response = _upload(client, auth_headers, owner_id, content=b"")
    assert response.status_code == 400


def test_unsafe_filename_is_stored_safely(client, auth_headers):
    owner_id = str(uuid4())
    response = _upload(client, auth_headers, owner_id, filename="../../etc/passwd", content=b"data")
    assert response.status_code == 201
    assert response.json()["filename"] == "../../etc/passwd"


def test_persistence_across_restart(storage_env, auth_headers):
    from fastapi.testclient import TestClient

    from app.main import app

    owner_id = str(uuid4())
    with TestClient(app) as client:
        upload = _upload(client, auth_headers, owner_id, content=b"persist-me", filename="p.txt")
        assert upload.status_code == 201
        object_id = upload.json()["id"]

    storage_env["storage_db"]._engine = None
    storage_env["storage_db"]._session_factory = None
    storage_env["storage"].reset_object_store()

    with TestClient(app) as client:
        download = client.get(
            f"/v1/objects/{object_id}",
            headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
        )
        assert download.status_code == 200
        assert download.content == b"persist-me"


def test_storage_health_endpoints(client, storage_env):
    health = client.get("/v1/storage/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["backend"] == "local"

    ready = client.get("/v1/storage/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"