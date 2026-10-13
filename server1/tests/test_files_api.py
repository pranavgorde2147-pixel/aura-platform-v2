"""Tests for the Server 1 file API and its Server 3 delegation boundary.

The storage engine is replaced with an in-memory fake so these tests run with
no external services while still exercising authorization, size limits and
error mapping.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import files
from app.dependencies.auth import get_current_user
from app.infrastructure.database.session import get_db
from app.infrastructure.storage_client import StorageServiceError


def make_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email="owner@example.com", is_active=True)


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error: StorageServiceError | None = None
        self.upload_result = {"id": str(uuid.uuid4()), "filename": "note.txt", "size_bytes": 3}
        self.list_result = {"items": [], "total": 0, "limit": 50, "offset": 0}
        self.metadata_result: dict | None = None
        self.delete_result = True
        self.download_headers = {"Content-Type": "text/plain", "Content-Disposition": "attachment"}
        self.download_body = b"hello"

    async def upload(self, **kwargs):
        self.calls.append(("upload", kwargs))
        if self.error:
            raise self.error
        return self.upload_result

    async def list_objects(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.error:
            raise self.error
        return self.list_result

    async def get_metadata(self, **kwargs):
        self.calls.append(("metadata", kwargs))
        if self.error:
            raise self.error
        return self.metadata_result

    async def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.error:
            raise self.error
        return self.delete_result

    async def stream_object(self, **kwargs):
        self.calls.append(("download", kwargs))
        if self.error:
            raise self.error

        async def iterator():
            yield self.download_body

        return self.download_headers, iterator()


def build_client(user: SimpleNamespace | None = None) -> TestClient:
    """Return a TestClient for an app that mounts only the files router."""
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1")

    async def _db():
        yield None

    app.dependency_overrides[get_db] = _db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def patch_storage(monkeypatch, storage: FakeStorage) -> None:
    monkeypatch.setattr(files, "get_storage_client", lambda: storage)
    monkeypatch.setattr(files, "_resolve_ai_identity_id", AsyncMock(return_value=None))


def test_upload_delegates_with_authenticated_owner(monkeypatch):
    storage = FakeStorage()
    user = make_user()
    patch_storage(monkeypatch, storage)

    response = build_client(user).post(
        "/api/v1/files",
        files={"file": ("note.txt", b"hi!", "text/plain")},
        data={"source_app": "notes"},
    )

    assert response.status_code == 201, response.text
    name, kwargs = storage.calls[0]
    assert name == "upload"
    assert kwargs["owner_id"] == str(user.id)
    assert kwargs["filename"] == "note.txt"
    assert kwargs["content"] == b"hi!"
    assert kwargs["source_app"] == "notes"


def test_upload_rejects_empty_payload(monkeypatch):
    storage = FakeStorage()
    patch_storage(monkeypatch, storage)

    response = build_client(make_user()).post(
        "/api/v1/files", files={"file": ("empty.txt", b"", "text/plain")}
    )

    assert response.status_code == 400
    assert storage.calls == []


def test_upload_enforces_size_limit(monkeypatch):
    storage = FakeStorage()
    patch_storage(monkeypatch, storage)
    monkeypatch.setattr(files, "get_settings", lambda: SimpleNamespace(max_upload_size_bytes=4))

    response = build_client(make_user()).post(
        "/api/v1/files", files={"file": ("big.txt", b"way too large", "text/plain")}
    )

    assert response.status_code == 413
    assert storage.calls == []


def test_upload_maps_storage_failure_to_503(monkeypatch):
    storage = FakeStorage()
    storage.error = StorageServiceError("Storage service unavailable")
    patch_storage(monkeypatch, storage)

    response = build_client(make_user()).post(
        "/api/v1/files", files={"file": ("note.txt", b"hi", "text/plain")}
    )

    assert response.status_code == 503


def test_list_uses_authenticated_owner(monkeypatch):
    storage = FakeStorage()
    user = make_user()
    patch_storage(monkeypatch, storage)

    response = build_client(user).get("/api/v1/files", params={"limit": 10, "offset": 5})

    assert response.status_code == 200
    name, kwargs = storage.calls[0]
    assert name == "list"
    assert kwargs["owner_id"] == str(user.id)
    assert kwargs["limit"] == 10
    assert kwargs["offset"] == 5


def test_metadata_returns_404_when_missing(monkeypatch):
    storage = FakeStorage()
    storage.metadata_result = None
    patch_storage(monkeypatch, storage)

    response = build_client(make_user()).get(f"/api/v1/files/{uuid.uuid4()}/metadata")

    assert response.status_code == 404


def test_delete_returns_404_when_missing(monkeypatch):
    storage = FakeStorage()
    storage.delete_result = False
    patch_storage(monkeypatch, storage)

    response = build_client(make_user()).delete(f"/api/v1/files/{uuid.uuid4()}")

    assert response.status_code == 404


def test_download_streams_content(monkeypatch):
    storage = FakeStorage()
    storage.download_body = b"binary-data"
    patch_storage(monkeypatch, storage)

    response = build_client(make_user()).get(f"/api/v1/files/{uuid.uuid4()}")

    assert response.status_code == 200
    assert response.content == b"binary-data"
    assert response.headers["content-type"].startswith("text/plain")


def test_files_require_authentication():
    response = build_client(None).post(
        "/api/v1/files", files={"file": ("note.txt", b"hi", "text/plain")}
    )
    assert response.status_code in (401, 403)
