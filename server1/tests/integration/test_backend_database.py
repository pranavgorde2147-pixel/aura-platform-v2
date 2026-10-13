"""Integration test running the real backend API against a real PostgreSQL.

Uses an embedded PostgreSQL (pgserver) instance and the actual application
(app.main.app) with its real SQLAlchemy repositories. This validates that
registration, authentication and user scoping genuinely persist to and read
from the database rather than mocks.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.database.session import configure_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402


@pytest.fixture(scope="module")
def database():
    data_dir = Path(tempfile.mkdtemp(prefix="aura-backend-pg-"))
    server = pgserver.get_server(data_dir)
    uri = server.get_uri()
    try:
        yield uri
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(database):
    import os

    sync_url = database.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ["POSTGRES_URL"] = sync_url
    get_settings.cache_clear()
    engine = configure_engine()

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Drop connections created on this setup loop so the app's own event
        # loop builds fresh asyncpg connections for TestClient.
        await engine.dispose()

    asyncio.run(_create_tables())

    with TestClient(app) as test_client:
        yield test_client

    asyncio.run(engine.dispose())
    os.environ.pop("POSTGRES_URL", None)
    get_settings.cache_clear()


def _register(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Integration User"},
    )
    assert response.status_code == 201, response.text


def _login(client: TestClient, email: str) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
def test_registration_login_and_identity_persistence(client):
    email = f"user-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)

    duplicate = client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Dup"},
    )
    assert duplicate.status_code == 400

    tokens = _login(client, email)
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email

    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


@pytest.mark.integration
def test_users_cannot_access_each_other(client):
    email_a = f"a-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"b-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)

    tokens_a = _login(client, email_a)
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}

    user_b = client.get(f"/api/v1/users/email/{email_b}", headers=headers_a)
    assert user_b.status_code == 403


@pytest.mark.integration
def test_app_registry_connection_lifecycle(client):
    email = f"apps-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    catalog = client.get("/api/v1/apps")
    assert catalog.status_code == 200
    assert any(entry["slug"] == "meeting" for entry in catalog.json())
    assert client.get("/api/v1/apps/meeting").json()["name"] == "AURA Meeting"
    assert client.get("/api/v1/apps/unknown-app").status_code == 404

    install = client.post("/api/v1/apps/me", json={"module_slug": "meeting"}, headers=headers)
    assert install.status_code == 201, install.text
    body = install.json()
    assert body["module_slug"] == "meeting"
    assert body["is_active"] is True
    assert len(body["permissions"]) == 3
    module_id = body["id"]

    listing = client.get("/api/v1/apps/me", headers=headers)
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [module_id]

    duplicate = client.post("/api/v1/apps/me", json={"module_slug": "meeting"}, headers=headers)
    assert duplicate.status_code == 409

    unknown = client.post("/api/v1/apps/me", json={"module_slug": "not-an-app"}, headers=headers)
    assert unknown.status_code == 404

    one = client.get(f"/api/v1/apps/me/{module_id}", headers=headers)
    assert one.status_code == 200 and one.json()["module_slug"] == "meeting"

    update = client.patch(
        f"/api/v1/apps/me/{module_id}",
        json={"is_active": False, "permissions": [{"scope": "brain", "action": "read"}]},
        headers=headers,
    )
    assert update.status_code == 200, update.text
    assert update.json()["is_active"] is False
    assert len(update.json()["permissions"]) == 1
    assert update.json()["permissions"][0]["scope"] == "brain"

    replaced = client.put(
        f"/api/v1/apps/me/{module_id}",
        json={"is_active": True},
        headers=headers,
    )
    assert replaced.status_code == 200 and replaced.json()["is_active"] is True

    deleted = client.delete(f"/api/v1/apps/me/{module_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/apps/me/{module_id}", headers=headers).status_code == 404


@pytest.mark.integration
def test_app_connections_are_isolated_per_user(client):
    email_a = f"appa-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"appb-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)

    headers_a = {"Authorization": f"Bearer {_login(client, email_a)['access_token']}"}
    headers_b = {"Authorization": f"Bearer {_login(client, email_b)['access_token']}"}

    created = client.post("/api/v1/apps/me", json={"module_slug": "brain"}, headers=headers_a)
    assert created.status_code == 201, created.text
    module_id = created.json()["id"]

    assert client.get("/api/v1/apps/me", headers=headers_b).json()["items"] == []
    assert client.get(f"/api/v1/apps/me/{module_id}", headers=headers_b).status_code == 404
    assert client.delete(f"/api/v1/apps/me/{module_id}", headers=headers_b).status_code == 404
    assert client.patch(f"/api/v1/apps/me/{module_id}", json={"is_active": False}, headers=headers_b).status_code == 404

    assert client.delete(f"/api/v1/apps/me/{module_id}", headers=headers_a).status_code == 204
