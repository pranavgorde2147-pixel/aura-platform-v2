"""Object lifecycle, ownership and persistence tests for Server 3."""

from __future__ import annotations

from uuid import uuid4


def _upload(client, headers, owner_id, content=b"hello world", filename="note.txt", content_type="text/plain"):
    return client.post(
        "/v1/objects",
        headers={**headers, "X-Aura-Owner-Id": owner_id},
        files={"file": (filename, content, content_type)},
        data={"source_app": "test"},
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

    listing = client.get(
        f"/v1/objects?owner_id={owner_id}",
        headers=auth_headers,
    )
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
    object_id = upload.json()["id"]

    # User B cannot read, list, or delete User A's object even knowing its id.
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

    # Object is still intact for its owner.
    assert client.get(
        f"/v1/objects/{object_id}",
        headers={**auth_headers, "X-Aura-Owner-Id": owner_a},
    ).status_code == 200


def test_size_limit_enforced(client, auth_headers):
    owner_id = str(uuid4())
    too_big = b"x" * (1048576 + 10)
    response = _upload(client, auth_headers, owner_id, content=too_big, filename="big.bin", content_type="application/octet-stream")
    assert response.status_code == 413


def test_content_type_allow_list(client, auth_headers, app_env):
    owner_id = str(uuid4())
    # Reconfigure the allow-list and reset cached settings/state.
    import os

    os.environ["ALLOWED_CONTENT_TYPES"] = "text/plain"
    app_env["config"].get_settings.cache_clear()

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
    # The storage key must not escape the owner prefix.
    assert response.json()["filename"] == "../../etc/passwd"


def test_persistence_across_restart(app_env, auth_headers):
    from app.main import create_app
    from fastapi.testclient import TestClient

    owner_id = str(uuid4())
    with TestClient(create_app()) as client:
        upload = _upload(client, auth_headers, owner_id, content=b"persist-me", filename="p.txt")
        assert upload.status_code == 201
        object_id = upload.json()["id"]

    # Simulate a process restart: dispose engine + object store cache, rebuild app.
    import asyncio

    app_env["database"]._engine = None
    app_env["database"]._session_factory = None
    app_env["storage"].reset_object_store()

    with TestClient(create_app()) as client:
        download = client.get(
            f"/v1/objects/{object_id}",
            headers={**auth_headers, "X-Aura-Owner-Id": owner_id},
        )
        assert download.status_code == 200
        assert download.content == b"persist-me"
