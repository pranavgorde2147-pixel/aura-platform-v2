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
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    from app.services.memory_index import get_memory_indexer

    sync_url = database.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ["POSTGRES_URL"] = sync_url
    os.environ["BRAIN_REMOTE_PROVISIONING_ENABLED"] = "false"
    # Keep integration tests hermetic: never reach a real Qdrant from tests.
    os.environ["MEMORY_VECTOR_SEARCH_ENABLED"] = "false"
    get_settings.cache_clear()
    get_memory_indexer.cache_clear()
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
    os.environ.pop("BRAIN_REMOTE_PROVISIONING_ENABLED", None)
    os.environ.pop("MEMORY_VECTOR_SEARCH_ENABLED", None)
    get_settings.cache_clear()
    get_memory_indexer.cache_clear()


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
def test_owner_can_reconcile_existing_brain_without_replacing_identity(
    client, monkeypatch
):
    from app.api.v1 import users as users_module

    email = f"reconcile-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    before = client.get("/api/v1/brain/me", headers=headers)
    assert before.status_code == 200
    assert before.json()["provisioning_status"] == "pending"

    calls = []

    class FakeBrainClient:
        async def provision_brain(self, **kwargs):
            calls.append(kwargs)
            return {
                "brain_id": kwargs["brain_id"],
                "owner_id": kwargs["owner_id"],
                "ai_identity_id": kwargs["ai_identity_id"],
                "provisioning_status": "ready",
                "resource_manifest_version": 1,
                "model_configuration": {
                    "model_id": kwargs["model_id"],
                    "runtime_model_id": kwargs["model_id"],
                    "runtime_mode": "dedicated",
                },
                "inference_runtime_status": "ready_simulated",
                "llm_runtime_id": f"rt_{uuid.uuid4().hex}",
                "llm_runtime_status": "ready_simulated",
                "runtime_isolation": "simulated",
                "model_configuration_id": f"{kwargs['model_id']}@1",
                "memory_resource_reference": f"brain_{kwargs['brain_id']}/personal_memory",
                "vector_resource_reference": f"brain_{kwargs['brain_id']}/vector_engine",
                "vault_resource_reference": f"brain_{kwargs['brain_id']}/personal_vault",
                "resource_root": f"brain_{kwargs['brain_id']}",
                "manifest_version": 1,
                "connected_app_count": 0,
                "compute_allocation": {
                    "max_concurrent_requests": kwargs["max_concurrent_requests"],
                    "concurrency_enforcement": "per-brain",
                },
                "storage_resource_reference": (
                    f"owners/{kwargs['owner_id']}/brains/{kwargs['brain_id']}"
                ),
                "request_count": 0,
                "last_health_check": "2026-10-22T00:00:00+00:00",
            }

    monkeypatch.setattr(
        users_module,
        "get_settings",
        lambda: SimpleNamespace(
            brain_remote_provisioning_enabled=True,
            ai_server_model="Qwen3.5-0.8B",
        ),
    )
    monkeypatch.setattr(users_module, "get_brain_client", FakeBrainClient)
    reconciled = client.post("/api/v1/brain/me/reconcile", headers=headers)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["provisioning_status"] == "ready"
    assert reconciled.json()["brain_id"] == before.json()["brain_id"]
    assert reconciled.json()["ai_identity_id"] == before.json()["ai_identity_id"]
    assert calls[0]["model_id"] == before.json()["model_configuration"]["model_id"]
    # The dedicated runtime assigned by Server 2 is projected onto the owner's
    # Brain row and reported back to the owner.
    assert reconciled.json()["llm_runtime_id"]
    assert reconciled.json()["runtime_isolation"] == "simulated"


@pytest.mark.integration
def test_brain_admin_requires_operator_allowlist(client, monkeypatch):
    from app.api.v1 import brain_admin as admin_module

    email = f"brain-admin-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    brain = client.get("/api/v1/brain/me", headers=headers).json()
    user_id = client.get("/api/v1/auth/me", headers=headers).json()["user_id"]

    monkeypatch.setattr(
        admin_module,
        "get_settings",
        lambda: SimpleNamespace(brain_admin_user_ids=""),
    )
    denied = client.get("/api/v1/admin/brains", headers=headers)
    assert denied.status_code == 403

    monkeypatch.setattr(
        admin_module,
        "get_settings",
        lambda: SimpleNamespace(brain_admin_user_ids=user_id),
    )
    allowed = client.get("/api/v1/admin/brains", headers=headers)
    assert allowed.status_code == 200, allowed.text
    payload = allowed.json()
    assert any(item["brain_id"] == brain["brain_id"] for item in payload["items"])
    assert "content" not in str(payload).lower()


@pytest.mark.integration
def test_refresh_rotates_and_reuse_revokes_all_sessions(client):
    email = f"rotate-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    tokens = _login(client, email)

    rotated = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200, rotated.text
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != tokens["refresh_token"]

    # Replaying the rotated token is treated as reuse and revokes every session.
    reused = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reused.status_code == 401

    after_reuse = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh}
    )
    assert after_reuse.status_code == 401


@pytest.mark.integration
def test_logout_invalidates_refresh_token(client):
    email = f"logout-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    logged_out = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=headers,
    )
    assert logged_out.status_code == 204, logged_out.text

    after_logout = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert after_logout.status_code == 401


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
    granted = {(permission["scope"], permission["action"]) for permission in body["permissions"]}
    assert ("brain", "read") in granted
    assert ("meeting", "write") in granted
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


@pytest.mark.integration
def test_brain_ingest_records_vault_knowledge_and_timeline(client):
    email = f"ingest-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    connect = client.post("/api/v1/apps/me", json={"module_slug": "meeting"}, headers=headers)
    assert connect.status_code == 201, connect.text

    before = client.get("/api/v1/brain/context", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["knowledge"]["node_count"] == 0
    assert before.json()["recent_timeline_count"] == 0
    assert before.json()["memory"]["object_reference"] is None

    ingest = client.post(
        "/api/v1/brain/ingest",
        headers=headers,
        json={
            "content": "Met Alice about the roadmap",
            "content_type": "text",
            "source_app": "meeting",
            "metadata": {
                "object_reference": "objects/note.txt",
                "mime_type": "text/plain",
                "file_size_bytes": 42,
            },
        },
    )
    assert ingest.status_code == 200, ingest.text
    body = ingest.json()
    assert body["accepted"] is True
    assert body["memory_id"]
    assert body["vault_entry_id"]

    after = client.get("/api/v1/brain/context", headers=headers).json()
    assert after["memory"]["object_reference"] == "objects/note.txt"
    assert after["memory"]["processing_status"] == "stored"
    assert after["knowledge"]["node_count"] >= 1
    assert after["recent_timeline_count"] >= 1


@pytest.mark.integration
def test_app_ingest_requires_connection_and_is_user_scoped(client):
    email_a = f"appa-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"appb-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)
    headers_a = {"Authorization": f"Bearer {_login(client, email_a)['access_token']}"}
    headers_b = {"Authorization": f"Bearer {_login(client, email_b)['access_token']}"}

    study_payload = {"notes": "Lecture on normalization", "external_id": "lecture-1"}

    # No app connection yet -> forbidden.
    denied = client.post("/api/v1/apps/study/ingest", json=study_payload, headers=headers_a)
    assert denied.status_code == 403, denied.text

    connect = client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers_a)
    assert connect.status_code == 201, connect.text

    accepted = client.post("/api/v1/apps/study/ingest", json=study_payload, headers=headers_a)
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["accepted"] is True and body["source_app"] == "study"
    assert body["memory_id"]
    assert body["duplicate"] is False

    # Replaying the same external_id must not create a second memory.
    replay = client.post("/api/v1/apps/study/ingest", json=study_payload, headers=headers_a)
    assert replay.status_code == 201
    assert replay.json()["duplicate"] is True
    assert replay.json()["memory_id"] == body["memory_id"]

    # Only one memory exists for user A.
    memories_a = client.get("/api/v1/brain/memory", headers=headers_a).json()
    assert len(memories_a) == 1

    # User B never authorized the app and cannot ingest or see A's data.
    assert client.post("/api/v1/apps/study/ingest", json=study_payload, headers=headers_b).status_code == 403
    assert client.get("/api/v1/brain/memory", headers=headers_b).json() == []


@pytest.mark.integration
def test_app_ingest_validates_payload_and_unknown_app(client):
    email = f"appv-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    # Unknown app slug -> 404.
    assert client.post("/api/v1/apps/not-an-app/ingest", json={}, headers=headers).status_code == 404

    connect = client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers)
    assert connect.status_code == 201

    # Missing required field -> 422 (and nothing persisted).
    invalid = client.post("/api/v1/apps/study/ingest", json={"title": "no body"}, headers=headers)
    assert invalid.status_code == 422, invalid.text
    assert client.get("/api/v1/brain/memory", headers=headers).json() == []


@pytest.mark.integration
def test_notification_ingest_records_timeline_without_memory(client):
    email = f"appn-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    connect = client.post("/api/v1/apps/me", json={"module_slug": "notifications"}, headers=headers)
    assert connect.status_code == 201, connect.text

    before = client.get("/api/v1/brain/context", headers=headers).json()
    ingest = client.post(
        "/api/v1/apps/notifications/ingest",
        json={"message": "Build finished", "level": "info", "external_id": "n1"},
        headers=headers,
    )
    assert ingest.status_code == 201, ingest.text
    assert ingest.json()["memory_id"] is None

    after = client.get("/api/v1/brain/context", headers=headers).json()
    assert after["recent_timeline_count"] == before["recent_timeline_count"] + 1
    assert client.get("/api/v1/brain/memory", headers=headers).json() == []


@pytest.mark.integration
def test_brain_ingest_without_object_leaves_vault_untouched(client):
    email = f"noobj-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    ingest = client.post(
        "/api/v1/brain/ingest",
        headers=headers,
        json={"content": "A plain text note", "content_type": "text"},
    )
    assert ingest.status_code == 200, ingest.text
    assert ingest.json()["vault_entry_id"] is None

    context = client.get("/api/v1/brain/context", headers=headers).json()
    assert context["memory"]["object_reference"] is None
    assert context["knowledge"]["node_count"] >= 1
    assert context["recent_timeline_count"] >= 1


CORE_APPS = ["study", "meeting", "health", "diary", "travel", "notifications"]


# --------------------------------------------------- registry completeness

@pytest.mark.integration
def test_core_apps_are_registered_with_brain_read(client):
    """Every required app is discoverable and declares a consistent capability."""
    catalog = client.get("/api/v1/apps").json()
    by_slug = {entry["slug"]: entry for entry in catalog}

    for slug in CORE_APPS:
        assert slug in by_slug, f"{slug} missing from catalog"
        entry = by_slug[slug]
        assert entry["name"] and entry["description"]

        one = client.get(f"/api/v1/apps/{slug}")
        assert one.status_code == 200
        assert one.json()["slug"] == slug

        if entry["queries_brain"]:
            scopes = {(p["scope"], p["action"]) for p in entry["default_permissions"]}
            assert ("brain", "read") in scopes, f"{slug} advertises queries_brain but lacks brain:read"

    # Notification Hub records events but must not read the brain.
    assert by_slug["notifications"]["queries_brain"] is False


@pytest.mark.integration
def test_aura_core_brain_query_requires_its_own_connection_and_brain_read(client):
    email = f"aura-core-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    catalog = client.get("/api/v1/apps").json()
    aura_core_entries = [entry for entry in catalog if entry["slug"] == "aura-core"]
    assert len(aura_core_entries) == 1
    aura_core = aura_core_entries[0]
    assert aura_core["name"] == "AURA Core"
    assert aura_core["client_type"] == "mobile"
    assert aura_core["queries_brain"] is True
    assert {(p["scope"], p["action"]) for p in aura_core["default_permissions"]} == {
        ("brain", "read")
    }

    # Registration in the catalog does not create a per-user connection.
    assert client.get("/api/v1/apps/me", headers=headers).json()["items"] == []
    denied_unconnected = client.post(
        "/api/v1/brain/query",
        json={"query": "hello", "source_app": "aura-core"},
        headers=headers,
    )
    assert denied_unconnected.status_code == 403

    arbitrary_grant = client.post(
        "/api/v1/apps/me",
        json={
            "module_slug": "aura-core",
            "permissions": [{"scope": "memory", "action": "write"}],
        },
        headers=headers,
    )
    assert arbitrary_grant.status_code == 403
    assert client.get("/api/v1/apps/me", headers=headers).json()["items"] == []

    connected_without_permission = client.post(
        "/api/v1/apps/me",
        json={
            "module_slug": "aura-core",
            "permissions": [{"scope": "identity", "action": "read"}],
        },
        headers=headers,
    )
    assert connected_without_permission.status_code == 201

    # A repeated connect does not reconcile or replace an existing grant set.
    already_connected = client.post(
        "/api/v1/apps/me",
        json={"module_slug": "aura-core"},
        headers=headers,
    )
    assert already_connected.status_code == 409

    denied_missing_permission = client.post(
        "/api/v1/brain/query",
        json={"query": "hello", "source_app": "aura-core"},
        headers=headers,
    )
    assert denied_missing_permission.status_code == 403

    # The client must fetch the existing connection and merge permissions:
    # PATCH replaces the full permission list rather than adding one grant.
    connections = client.get("/api/v1/apps/me", headers=headers).json()["items"]
    core_connection = next(item for item in connections if item["module_slug"] == "aura-core")
    assert {
        (permission["scope"], permission["action"])
        for permission in core_connection["permissions"]
    } == {("identity", "read")}

    unauthorized_update = client.patch(
        f"/api/v1/apps/me/{core_connection['id']}",
        json={
            "is_active": False,
            "permissions": [
                {"scope": "identity", "action": "read"},
                {"scope": "brain", "action": "read"},
                {"scope": "memory", "action": "write"},
            ],
        },
        headers=headers,
    )
    assert unauthorized_update.status_code == 403
    still_unchanged = client.get(
        f"/api/v1/apps/me/{core_connection['id']}", headers=headers
    )
    assert still_unchanged.status_code == 200
    assert still_unchanged.json()["is_active"] is True
    assert {
        (permission["scope"], permission["action"])
        for permission in still_unchanged.json()["permissions"]
    } == {("identity", "read")}

    granted = client.patch(
        f"/api/v1/apps/me/{core_connection['id']}",
        json={
            "permissions": [
                {"scope": permission["scope"], "action": permission["action"]}
                for permission in core_connection["permissions"]
            ]
            + [{"scope": "brain", "action": "read"}]
        },
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    assert {
        (permission["scope"], permission["action"])
        for permission in granted.json()["permissions"]
    } == {
        ("identity", "read"),
        ("brain", "read"),
    }

    # The query succeeds only once the same user's connection grants brain:read.
    result = client.post(
        "/api/v1/brain/query",
        json={"query": "hello from AURA Core", "source_app": "aura-core"},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    assert result.json()["answer"]

    connections = client.get("/api/v1/apps/me", headers=headers).json()["items"]
    core_connection = next(item for item in connections if item["module_slug"] == "aura-core")
    assert {
        (permission["scope"], permission["action"])
        for permission in core_connection["permissions"]
    } == {
        ("identity", "read"),
        ("brain", "read"),
    }


@pytest.mark.integration
def test_catalog_registration_is_idempotent(client):
    """Repeated catalog reads are stable: no duplicates, stable ordering."""
    first = client.get("/api/v1/apps").json()
    second = client.get("/api/v1/apps").json()
    assert first == second
    slugs = [entry["slug"] for entry in first]
    assert len(slugs) == len(set(slugs))
    assert slugs == sorted(slugs)


# ------------------------------------------------- brain query authorization

@pytest.mark.integration
def test_brain_query_authorized_after_connecting_core_apps(client):
    """Connecting an app grants brain:read, unblocking POST /brain/query."""
    email = f"query-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    # Before connecting, a named source_app is rejected.
    denied = client.post(
        "/api/v1/brain/query",
        json={"query": "hi", "source_app": "study"},
        headers=headers,
    )
    assert denied.status_code == 403, denied.text

    for slug in ("aura-core", "study", "meeting", "diary", "travel", "health"):
        connect = client.post("/api/v1/apps/me", json={"module_slug": slug}, headers=headers)
        assert connect.status_code == 201, connect.text
        if slug == "aura-core":
            assert {
                (permission["scope"], permission["action"])
                for permission in connect.json()["permissions"]
            } == {("brain", "read")}
        elif slug in {"study", "diary"}:
            assert {
                (permission["scope"], permission["action"])
                for permission in connect.json()["permissions"]
            } == {
                ("brain", "read"),
                ("memory", "write"),
                ("timeline", "write"),
                (slug, "write"),
            }

        result = client.post(
            "/api/v1/brain/query",
            json={"query": f"status from {slug}", "source_app": slug},
            headers=headers,
        )
        assert result.status_code == 200, result.text
        assert result.json()["answer"]

    # An app the user never connected still cannot query.
    other = client.post(
        "/api/v1/brain/query",
        json={"query": "hi", "source_app": "search"},
        headers=headers,
    )
    assert other.status_code == 403, other.text

    # Connection endpoint reports the granted brain:read permission.
    listing = client.get("/api/v1/apps/me", headers=headers).json()["items"]
    study = next(item for item in listing if item["module_slug"] == "study")
    assert any(p["scope"] == "brain" and p["action"] == "read" for p in study["permissions"])


@pytest.mark.integration
def test_deactivating_connection_blocks_brain_query(client):
    email = f"deact-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    connect = client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers)
    assert connect.status_code == 201
    module_id = connect.json()["id"]

    ok = client.post(
        "/api/v1/brain/query", json={"query": "hi", "source_app": "study"}, headers=headers
    )
    assert ok.status_code == 200

    off = client.patch(
        f"/api/v1/apps/me/{module_id}", json={"is_active": False}, headers=headers
    )
    assert off.status_code == 200

    blocked = client.post(
        "/api/v1/brain/query", json={"query": "hi", "source_app": "study"}, headers=headers
    )
    assert blocked.status_code == 403, blocked.text


@pytest.mark.integration
def test_brain_query_authorization_is_user_scoped(client):
    email_a = f"qa-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"qb-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)
    headers_a = {"Authorization": f"Bearer {_login(client, email_a)['access_token']}"}
    headers_b = {"Authorization": f"Bearer {_login(client, email_b)['access_token']}"}

    connect = client.post("/api/v1/apps/me", json={"module_slug": "meeting"}, headers=headers_a)
    assert connect.status_code == 201

    # User A can query via meeting; user B (no connection) cannot.
    assert client.post(
        "/api/v1/brain/query", json={"query": "hi", "source_app": "meeting"}, headers=headers_a
    ).status_code == 200
    assert client.post(
        "/api/v1/brain/query", json={"query": "hi", "source_app": "meeting"}, headers=headers_b
    ).status_code == 403


# ------------------------------------------------------ ingest for six apps

def _app_ingest_payload(slug: str) -> dict:
    payloads = {
        "study": {"notes": "Lecture on indexing", "external_id": f"study-{uuid.uuid4().hex[:6]}"},
        "meeting": {"summary": "Roadmap sync", "external_id": f"meet-{uuid.uuid4().hex[:6]}"},
        "health": {"content": "Ran 5k", "external_id": f"health-{uuid.uuid4().hex[:6]}"},
        "diary": {"entry": "A calm day", "external_id": f"diary-{uuid.uuid4().hex[:6]}"},
        "travel": {"destination": "Goa", "external_id": f"trip-{uuid.uuid4().hex[:6]}"},
        "notifications": {"message": "Build ok", "external_id": f"notif-{uuid.uuid4().hex[:6]}"},
    }
    return payloads[slug]


@pytest.mark.integration
def test_core_apps_ingest_after_connection(client):
    email = f"ing6-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    for slug in CORE_APPS:
        connect = client.post("/api/v1/apps/me", json={"module_slug": slug}, headers=headers)
        assert connect.status_code == 201, (slug, connect.text)

        ingest = client.post(
            f"/api/v1/apps/{slug}/ingest",
            json=_app_ingest_payload(slug),
            headers=headers,
        )
        assert ingest.status_code == 201, (slug, ingest.text)
        body = ingest.json()
        assert body["accepted"] is True and body["source_app"] == slug
        # Notifications record an event without creating a memory.
        if slug == "notifications":
            assert body["memory_id"] is None
        else:
            assert body["memory_id"]


@pytest.mark.integration
def test_diary_record_is_retrieved_for_aura_core_query(client, monkeypatch):
    email_a = f"diary-query-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"diary-isolation-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)
    headers_a = {"Authorization": f"Bearer {_login(client, email_a)['access_token']}"}
    headers_b = {"Authorization": f"Bearer {_login(client, email_b)['access_token']}"}

    for slug in ("diary", "aura-core"):
        connected = client.post(
            "/api/v1/apps/me", json={"module_slug": slug}, headers=headers_a
        )
        assert connected.status_code == 201, connected.text
    client.post("/api/v1/apps/me", json={"module_slug": "aura-core"}, headers=headers_b)

    entry = ("A regular note about my day. " * 8) + "My pet name is Sai."
    saved = client.post(
        "/api/v1/apps/diary/ingest",
        json={"entry": entry, "external_id": "diary-pet-name-1"},
        headers=headers_a,
    )
    assert saved.status_code == 201, saved.text
    saved_body = saved.json()
    assert saved_body["accepted"] is True
    assert saved_body["record_id"]

    class ContextAwareTestProvider:
        def __init__(self):
            self.context = None

        async def prepare_external_context(self, user, query, context):
            return {"user_id": str(user.id), "query": query}

        async def process_query(self, user, query, context):
            self.context = context
            records = context.get("authorized_records", [])
            found = any("my pet name is sai" in record["content"].lower() for record in records)
            return {
                "answer": "Your pet name is Sai." if found else "I do not know your pet name.",
                "source": "integration_test",
            }

        async def personalize_response(self, user, answer, context):
            return answer

    provider = ContextAwareTestProvider()
    from app.services.personal_brain import PersonalBrainService

    monkeypatch.setattr(
        "app.api.v1.brain.PersonalBrainService",
        lambda session: PersonalBrainService(session, llm_provider=provider),
    )

    # Omit use_memory: Brain queries retrieve authorized memory/records by default.
    answer = client.post(
        "/api/v1/brain/query",
        json={"query": "What is my pet name?", "source_app": "aura-core"},
        headers=headers_a,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert "Sai" in body["answer"]
    assert body["memories_used"] == []
    assert len(body["records_used"]) == 1
    assert body["records_used"][0]["id"] == saved_body["record_id"]
    assert body["records_used"][0]["source_app"] == "diary"
    assert body["records_used"][0]["source_reference"] == "diary-pet-name-1"
    assert "My pet name is Sai." in body["records_used"][0]["content"]
    assert provider.context["authorized_records"][0]["source_app"] == "diary"

    # Same-user ownership filter applies regardless of source app or known record id.
    other_user_answer = client.post(
        "/api/v1/brain/query",
        json={"query": "What is my pet name?", "source_app": "aura-core"},
        headers=headers_b,
    )
    assert other_user_answer.status_code == 200, other_user_answer.text
    assert other_user_answer.json()["records_used"] == []
    assert "Sai" not in other_user_answer.json()["answer"]

    unauthorized_query = client.post(
        "/api/v1/brain/query",
        json={"query": "What is my pet name?", "source_app": "notifications"},
        headers=headers_a,
    )
    assert unauthorized_query.status_code == 403


@pytest.mark.integration
def test_diary_ingestion_reports_canonical_record_persistence_failure(client, monkeypatch):
    email = f"diary-failure-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}
    connected = client.post(
        "/api/v1/apps/me", json={"module_slug": "diary"}, headers=headers
    )
    assert connected.status_code == 201, connected.text

    async def fail_record_write(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(
        "app.services.personal_brain.PersonalBrainService._store_application_record",
        fail_record_write,
    )
    response = client.post(
        "/api/v1/apps/diary/ingest",
        json={"entry": "My pet name is Sai."},
        headers=headers,
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "The application record could not be durably stored"


@pytest.mark.integration
def test_shared_record_api_enforces_source_permissions_and_links_without_copying(client):
    email_a = f"shared-record-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"shared-record-other-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email_a)
    _register(client, email_b)
    headers_a = {"Authorization": f"Bearer {_login(client, email_a)['access_token']}"}
    headers_b = {"Authorization": f"Bearer {_login(client, email_b)['access_token']}"}

    diary_permissions = [
        {"scope": "memory", "action": "write"},
        {"scope": "diary", "action": "write"},
        {"scope": "diary", "action": "read"},
        {"scope": "diary", "action": "delete"},
        {"scope": "records", "action": "delete"},
    ]
    core_permissions = [
        {"scope": "brain", "action": "read"},
        {"scope": "records", "action": "read"},
        {"scope": "records", "action": "write"},
        {"scope": "records", "action": "delete"},
    ]
    study_permissions = [
        {"scope": "records", "action": "read"},
        {"scope": "study", "action": "read"},
        {"scope": "study", "action": "write"},
    ]
    connection_ids = {}
    for slug, permissions in (
        ("diary", diary_permissions),
        ("aura-core", core_permissions),
        ("study", study_permissions),
    ):
        connected = client.post(
            "/api/v1/apps/me",
            json={"module_slug": slug, "permissions": permissions},
            headers=headers_a,
        )
        assert connected.status_code == 201, connected.text
        connection_ids[slug] = connected.json()["id"]
    core_b = client.post(
        "/api/v1/apps/me",
        json={"module_slug": "aura-core", "permissions": core_permissions},
        headers=headers_b,
    )
    assert core_b.status_code == 201, core_b.text

    saved = client.post(
        "/api/v1/apps/diary/ingest",
        json={"entry": "Professor explained database normalization.", "external_id": "shared-record-1"},
        headers=headers_a,
    )
    assert saved.status_code == 201, saved.text
    record_id = saved.json()["record_id"]

    listing = client.get(
        "/api/v1/records",
        params={"client_app": "aura-core", "source_app": "diary"},
        headers=headers_a,
    )
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1
    assert listing.json()[0]["id"] == record_id
    assert listing.json()[0]["content"] is None
    assert listing.json()[0]["source_app"] == "diary"

    search = client.get(
        "/api/v1/records/search",
        params={"client_app": "aura-core", "q": "database normalization", "source_app": "diary"},
        headers=headers_a,
    )
    assert search.status_code == 200, search.text
    assert search.json()[0]["id"] == record_id

    detail = client.get(
        f"/api/v1/records/{record_id}",
        params={"client_app": "aura-core"},
        headers=headers_a,
    )
    assert detail.status_code == 200, detail.text
    assert "Professor explained" in detail.json()["content"]

    linked = client.put(
        f"/api/v1/records/{record_id}/links",
        params={"client_app": "aura-core"},
        json={"target_app": "study", "relationship": "study_reference"},
        headers=headers_a,
    )
    assert linked.status_code == 200, linked.text
    duplicate_link = client.put(
        f"/api/v1/records/{record_id}/links",
        params={"client_app": "aura-core"},
        json={"target_app": "study", "relationship": "study_reference"},
        headers=headers_a,
    )
    assert duplicate_link.status_code == 200, duplicate_link.text
    assert duplicate_link.json()["id"] == linked.json()["id"]

    study_view = client.get(
        "/api/v1/records",
        params={"client_app": "study", "destination_app": "study"},
        headers=headers_a,
    )
    assert study_view.status_code == 200, study_view.text
    assert len(study_view.json()) == 1
    assert study_view.json()[0]["id"] == record_id
    assert study_view.json()[0]["source_app"] == "diary"

    related = client.get(
        f"/api/v1/records/{record_id}/related",
        params={"client_app": "study"},
        headers=headers_a,
    )
    assert related.status_code == 200, related.text
    assert related.json()[0]["target_app"] == "study"

    unauthorized_source = client.get(
        "/api/v1/records",
        params={"client_app": "aura-core", "source_app": "diary"},
        headers=headers_b,
    )
    assert unauthorized_source.status_code == 403

    diary_inactive = client.patch(
        f"/api/v1/apps/me/{connection_ids['diary']}",
        json={"is_active": False},
        headers=headers_a,
    )
    assert diary_inactive.status_code == 200, diary_inactive.text
    unavailable_record = client.get(
        f"/api/v1/records/{record_id}",
        params={"client_app": "aura-core"},
        headers=headers_a,
    )
    assert unavailable_record.status_code == 404
    diary_active = client.patch(
        f"/api/v1/apps/me/{connection_ids['diary']}",
        json={"is_active": True},
        headers=headers_a,
    )
    assert diary_active.status_code == 200, diary_active.text

    foreign = client.get(
        f"/api/v1/records/{record_id}",
        params={"client_app": "aura-core"},
        headers=headers_b,
    )
    assert foreign.status_code == 404

    deleted = client.delete(
        f"/api/v1/records/{record_id}",
        params={"client_app": "diary"},
        headers=headers_a,
    )
    assert deleted.status_code == 204, deleted.text
    after_delete = client.get(
        f"/api/v1/records/{record_id}",
        params={"client_app": "aura-core"},
        headers=headers_a,
    )
    assert after_delete.status_code == 404


# ------------------------------------------- universal brain processing

def _fake_analysis_envelope(category: str, record_type: str, text: str, confidence: float) -> dict:
    """A Server 2 analysis response used to keep the test hermetic."""
    return {
        "analysis_mode": "llm",
        "confidence_kind": "model",
        "provider": "test",
        "status": "ok",
        "language": None,
        "latency_ms": 3,
        "degraded": False,
        "degraded_reason": None,
        "request_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "segments": [
            {
                "index": 0,
                "category": category,
                "record_type": record_type,
                "text": text,
                "summary": text[:200],
                "entities": [],
                "occurred_at": None,
                "confidence": confidence,
                "rationale": "integration fixture",
            }
        ],
    }


@pytest.mark.integration
def test_brain_process_routes_to_an_authorized_destination(client, monkeypatch, database):
    email = f"process-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    connect = client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers)
    assert connect.status_code == 201, connect.text

    envelope = _fake_analysis_envelope(
        "lecture", "lecture_note", "Lecture on normalisation", 0.93
    )
    fake_client = SimpleNamespace(analyze=AsyncMock(return_value=envelope))
    monkeypatch.setattr(
        "app.services.content_analysis.get_brain_client", lambda: fake_client
    )

    response = client.post(
        "/api/v1/brain/process",
        json={
            "content": "Lecture on normalisation",
            "source_app": "study",
            "input_id": "lecture-1",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "delivered"
    assert body["review_required"] is False
    assert body["classification"]["analysis_mode"] == "llm"
    assert body["classification"]["confidence_kind"] == "model"
    assert body["classification"]["categories"] == ["lecture"]
    assert body["items"][0]["destination_app"] == "study"
    assert body["items"][0]["status"] == "delivered"
    assert body["delivery"][0]["status"] == "delivered"
    assert body["record_references"]

    # The full content is stored as an application record, not as a memory.
    import psycopg

    with psycopg.connect(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM app_records WHERE destination_app = 'study'"
            )
            stored = cursor.fetchone()[0]
    assert stored >= 1


@pytest.mark.integration
def test_brain_process_is_idempotent_per_user(client, monkeypatch):
    email = f"idem-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}
    client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers)

    envelope = _fake_analysis_envelope("lecture", "lecture_note", "Lecture", 0.9)
    fake_client = SimpleNamespace(analyze=AsyncMock(return_value=envelope))
    monkeypatch.setattr(
        "app.services.content_analysis.get_brain_client", lambda: fake_client
    )

    payload = {
        "content": "Lecture on normalisation",
        "source_app": "study",
        "input_id": "lecture-idem",
    }
    first = client.post(
        "/api/v1/brain/process",
        json=payload,
        headers={**headers, "Idempotency-Key": "key-1"},
    )
    second = client.post(
        "/api/v1/brain/process",
        json=payload,
        headers={**headers, "Idempotency-Key": "key-1"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["processing_id"] == second.json()["processing_id"]


@pytest.mark.integration
def test_brain_process_rejects_an_unconnected_source_app(client, monkeypatch):
    email = f"denied-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}

    envelope = _fake_analysis_envelope("lecture", "lecture_note", "Lecture", 0.9)
    fake_client = SimpleNamespace(analyze=AsyncMock(return_value=envelope))
    monkeypatch.setattr(
        "app.services.content_analysis.get_brain_client", lambda: fake_client
    )

    response = client.post(
        "/api/v1/brain/process",
        json={"content": "Lecture", "source_app": "study", "input_id": "lecture-denied"},
        headers=headers,
    )
    assert response.status_code == 403, response.text


@pytest.mark.integration
def test_brain_process_parks_low_confidence_items_for_review(client, monkeypatch):
    email = f"review-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email)
    headers = {"Authorization": f"Bearer {_login(client, email)['access_token']}"}
    client.post("/api/v1/apps/me", json={"module_slug": "study"}, headers=headers)

    envelope = _fake_analysis_envelope("lecture", "lecture_note", "maybe a lecture", 0.55)
    fake_client = SimpleNamespace(analyze=AsyncMock(return_value=envelope))
    monkeypatch.setattr(
        "app.services.content_analysis.get_brain_client", lambda: fake_client
    )

    response = client.post(
        "/api/v1/brain/process",
        json={"content": "maybe a lecture", "source_app": "study", "input_id": "lecture-low"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "awaiting_review"
    assert body["review_required"] is True
    assert body["items"][0]["requires_review"] is True

    confirmed = client.post(
        f"/api/v1/brain/process/{body['processing_id']}/confirm",
        json={"items": [{"index": 0}]},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "delivered"
