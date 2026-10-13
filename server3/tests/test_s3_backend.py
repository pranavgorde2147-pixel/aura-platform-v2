"""S3-compatible backend tests using moto.

These verify the same object workflow against a mocked S3 endpoint.
"""

from __future__ import annotations

import io
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

TOKEN = "test-storage-token"


@pytest.fixture()
def s3_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("STORAGE_API_TOKEN", TOKEN)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("STORAGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'storage.db'}")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.amazonaws.com")
    monkeypatch.setenv("S3_BUCKET", "aura-test-bucket")
    monkeypatch.setenv("S3_ACCESS_KEY", "testing")
    monkeypatch.setenv("S3_SECRET_KEY", "testing")

    from app import config, storage

    config.get_settings.cache_clear()
    storage.reset_object_store()

    with mock_aws():
        from app.main import create_app

        with TestClient(create_app()) as client:
            yield client


def test_s3_upload_download_delete(s3_client):
    owner_id = str(uuid4())
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": owner_id}

    upload = s3_client.post(
        "/v1/objects",
        headers=headers,
        files={"file": ("report.txt", b"s3-content", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    record = upload.json()
    assert record["storage_backend"] == "s3"

    download = s3_client.get(f"/v1/objects/{record['id']}", headers=headers)
    assert download.status_code == 200
    assert download.content == b"s3-content"

    assert s3_client.delete(f"/v1/objects/{record['id']}", headers=headers).status_code == 200
    assert s3_client.get(f"/v1/objects/{record['id']}/metadata", headers=headers).status_code == 404
