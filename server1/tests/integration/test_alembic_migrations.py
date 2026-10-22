"""Regression tests for the schema-provisioning root cause.

Production returned HTTP 500 for both registration and login because the
container never ran migrations: PostgreSQL was reachable (``SELECT 1`` passed,
so health looked green) but the ``users`` table did not exist.

These tests run the *real* Alembic upgrade chain (in a subprocess, exactly like
the container command) against a real PostgreSQL and prove that:

* the migrated schema supports the full first-user journey, and
* the database health check now detects an unmigrated database instead of
  reporting it healthy.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.database.health import check_database_health  # noqa: E402
from app.infrastructure.database.session import (  # noqa: E402
    configure_engine,
    get_db,
    get_engine,
)
from app.main import app  # noqa: E402

SERVER_ROOT = Path(__file__).resolve().parents[2]


async def _dispose_engine() -> None:
    await get_engine().dispose()


async def _check_health_once() -> bool:
    """Run the database health check once, then dispose the engine.

    Disposing inside the same event loop avoids reusing asyncpg connections on
    a different loop across successive ``asyncio.run`` calls.
    """
    async for session in get_db():
        result = await check_database_health(session)
        break
    else:  # pragma: no cover - generator always yields
        result = False
    await _dispose_engine()
    return result


@pytest.fixture(scope="module")
def pg_server():
    data_dir = Path(tempfile.mkdtemp(prefix="aura-alembic-pg-"))
    server = pgserver.get_server(data_dir)
    try:
        yield server
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def fresh_database(pg_server):
    import psycopg

    name = f"aura_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.get_uri(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield pg_server.get_uri(database=name)
    finally:
        with psycopg.connect(pg_server.get_uri(), autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(autouse=True)
def restore_environment():
    """Restore process env/caches so migrated-DB tests cannot leak into others."""
    saved = {
        key: os.environ.get(key)
        for key in (
            "POSTGRES_URL",
            "MEMORY_VECTOR_SEARCH_ENABLED",
            "BRAIN_REMOTE_PROVISIONING_ENABLED",
        )
    }
    os.environ["BRAIN_REMOTE_PROVISIONING_ENABLED"] = "false"
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    try:
        asyncio.run(_dispose_engine())
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def _migrate(database_url: str) -> None:
    """Run ``alembic upgrade head`` in a subprocess, like the container does."""
    env = os.environ.copy()
    env["POSTGRES_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"alembic upgrade head failed\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )


def _use_database(database_url: str) -> None:
    os.environ["POSTGRES_URL"] = database_url
    os.environ["MEMORY_VECTOR_SEARCH_ENABLED"] = "false"
    get_settings.cache_clear()
    configure_engine()


@pytest.mark.integration
def test_migrated_schema_supports_full_first_user_journey(fresh_database):
    _migrate(fresh_database)
    _use_database(fresh_database)

    email = f"mig-{uuid.uuid4().hex[:8]}@example.com"
    with TestClient(app) as client:
        # The migrated database must now report a healthy, schema-aware status.
        database_status = client.get("/api/v1/health").json()["services"]["database"]
        assert database_status["status"] == "connected", database_status

        register = client.post(
            "/api/v1/users",
            json={"email": email, "password": "password123", "full_name": "Migrated User"},
        )
        assert register.status_code == 201, register.text

        duplicate = client.post(
            "/api/v1/users",
            json={"email": email, "password": "password123"},
        )
        assert duplicate.status_code == 400

        login = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "password123"}
        )
        assert login.status_code == 200, login.text
        tokens = login.json()

        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["email"] == email


@pytest.mark.integration
def test_health_detects_unmigrated_database(fresh_database):
    # Reachable but empty is the exact production failure mode.
    _use_database(fresh_database)
    assert asyncio.run(_check_health_once()) is False

    _migrate(fresh_database)
    _use_database(fresh_database)
    assert asyncio.run(_check_health_once()) is True


@pytest.mark.integration
def test_migrated_schema_provisions_identity_brain_and_memory_isolation(fresh_database):
    """The migrated schema must support the full first-user journey plus isolation.

    Registration must create exactly one default identity and one Personal Brain
    per user, and a second user must never be able to read the first user's
    memories or profile through the real API.
    """
    _migrate(fresh_database)
    _use_database(fresh_database)

    email_a = f"iso-a-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"iso-b-{uuid.uuid4().hex[:8]}@example.com"
    password = "password123"

    with TestClient(app) as client:
        for email in (email_a, email_b):
            assert (
                client.post(
                    "/api/v1/users",
                    json={"email": email, "password": password, "full_name": "Iso User"},
                ).status_code
                == 201
            )

        token_a = client.post(
            "/api/v1/auth/login", json={"email": email_a, "password": password}
        ).json()["access_token"]
        token_b = client.post(
            "/api/v1/auth/login", json={"email": email_b, "password": password}
        ).json()["access_token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        brain_a = client.get("/api/v1/brain/me", headers=headers_a)
        brain_b = client.get("/api/v1/brain/me", headers=headers_b)
        assert brain_a.status_code == brain_b.status_code == 200
        assert brain_a.json()["brain_id"] != brain_b.json()["brain_id"]
        assert brain_a.json()["ai_identity_id"] != brain_b.json()["ai_identity_id"]
        assert brain_a.json()["provisioning_status"] == "pending"

        # The Personal Brain binding exists for the authenticated user.
        context = client.get("/api/v1/brain/context", headers=headers_a)
        assert context.status_code == 200, context.text
        assert context.json()["brain"]["id"]

        # User A stores a fact; user B cannot see it anywhere.
        created = client.post(
            "/api/v1/brain/memory",
            headers=headers_a,
            json={"content": "User A's private fact", "kind": "fact"},
        )
        assert created.status_code == 201, created.text
        memory_id = created.json()["id"]

        assert client.get("/api/v1/brain/memory", headers=headers_b).json() == []
        assert client.get(f"/api/v1/brain/memory/{memory_id}", headers=headers_b).status_code == 404
        assert (
            client.delete(f"/api/v1/brain/memory/{memory_id}", headers=headers_b).status_code
            == 404
        )

        # The owner can still read it.
        assert client.get(f"/api/v1/brain/memory/{memory_id}", headers=headers_a).status_code == 200


@pytest.mark.integration
def test_public_registration_can_be_locked_down(fresh_database, monkeypatch):
    _migrate(fresh_database)
    _use_database(fresh_database)

    import app.api.v1.users as users_module
    from types import SimpleNamespace

    monkeypatch.setattr(
        users_module,
        "get_settings",
        lambda: SimpleNamespace(
            allow_public_registration=False,
            bootstrap_token="bootstrap-secret",
            brain_remote_provisioning_enabled=False,
        ),
    )

    email = f"locked-{uuid.uuid4().hex[:8]}@example.com"
    payload = {"email": email, "password": "password123"}
    with TestClient(app) as client:
        assert client.post("/api/v1/users", json=payload).status_code == 403
        blocked = client.post(
            "/api/v1/users", json=payload, headers={"X-Bootstrap-Token": "wrong"}
        )
        assert blocked.status_code == 403

        allowed = client.post(
            "/api/v1/users",
            json=payload,
            headers={"X-Bootstrap-Token": "bootstrap-secret"},
        )
        assert allowed.status_code == 201, allowed.text
