"""Tests for the AURA application registry and user app connections."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.installed_module import InstalledModule, PlatformType
from app.main import create_app
from app.services.app_registry import (
    AppAlreadyInstalledError,
    AppConnectionService,
    UnknownAppError,
    list_catalog,
)
from app.core.security import hash_password
from app.models.user import User


def make_user(email: str = "app-user@example.com") -> User:
    user = User(email=email, password_hash=hash_password("password123"), full_name="App User", is_active=True)
    return user


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows or []

    def scalars(self):
        return _FakeScalars(self._rows)

    def scalar(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Scripted async session: each execute() pops the next configured result."""

    def __init__(self, results=None):
        self.results = [r if r is not None else [] for r in (results or [])]
        self.added: list = []
        self.deleted: list = []

    async def execute(self, stmt):
        if self.results:
            return _FakeResult(self.results.pop(0))
        return _FakeResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        return obj

    async def delete(self, obj):
        self.deleted.append(obj)


# ---------------------------------------------------------------- catalog

def test_catalog_is_populated_with_unique_slugs():
    catalog = list_catalog()
    assert len(catalog) >= 10
    slugs = [app.slug for app in catalog]
    assert len(slugs) == len(set(slugs))
    assert "aura-core" in slugs
    assert "meeting" in slugs
    assert "chronos" in slugs


def test_aura_core_registry_requires_only_brain_read_for_queries():
    catalog = {app.slug: app for app in list_catalog()}
    aura_core = catalog["aura-core"]

    assert aura_core.queries_brain is True
    assert {(permission.scope, permission.action) for permission in aura_core.default_permissions} == {
        ("brain", "read")
    }


def test_catalog_endpoints_are_public():
    app = create_app()
    with TestClient(app) as client:
        catalog = client.get("/api/v1/apps")
        assert catalog.status_code == 200
        slugs = [entry["slug"] for entry in catalog.json()]
        assert "meeting" in slugs

        meeting = client.get("/api/v1/apps/meeting")
        assert meeting.status_code == 200
        assert meeting.json()["name"] == "AURA Meeting"
        assert meeting.json()["queries_brain"] is True

        missing = client.get("/api/v1/apps/does-not-exist")
        assert missing.status_code == 404


# ----------------------------------------------------- connection service

def make_installed_module(user, slug="meeting", name="AURA Meeting") -> InstalledModule:
    return InstalledModule(
        user_id=user.id,
        module_slug=slug,
        module_name=name,
        platform_type=PlatformType.AURA,
        is_active=True,
    )


def test_install_grants_default_permissions():
    user = make_user()
    session = FakeSession(results=[[]])  # no existing connection
    service = AppConnectionService(session)

    module = asyncio.run(service.install(user, "meeting"))

    assert isinstance(module, InstalledModule)
    assert module.module_slug == "meeting"
    perms = module.module_permissions
    assert len(perms) >= 3
    scopes = {p.scope for p in perms}
    assert "brain" in scopes and "files" in scopes


def test_install_unknown_slug_raises():
    user = make_user()
    service = AppConnectionService(FakeSession(results=[[]]))
    with pytest.raises(UnknownAppError):
        asyncio.run(service.install(user, "not-an-app"))


def test_reinstall_raises_already_installed():
    user = make_user()
    existing = make_installed_module(user)
    session = FakeSession(results=[[existing]])
    service = AppConnectionService(session)
    with pytest.raises(AppAlreadyInstalledError):
        asyncio.run(service.install(user, "meeting"))


def test_install_custom_permissions_replace_defaults():
    user = make_user()
    session = FakeSession(results=[[]])
    service = AppConnectionService(session)
    from app.schemas.apps import ModulePermissionSpec

    module = asyncio.run(
        service.install(user, "brain", permissions=[ModulePermissionSpec(scope="memory", action="read")])
    )
    perms = module.module_permissions
    assert len(perms) == 1
    assert perms[0].scope == "memory"
    assert perms[0].permission.value == "read"


def test_get_connection_is_scoped_to_user():
    user_a = make_user()
    user_b = make_user("other@example.com")
    module_a = make_installed_module(user_a)
    session = FakeSession(results=[[], []])  # user B sees no rows
    service = AppConnectionService(session)
    assert asyncio.run(service.get_connection(user_b, module_a.id)) is None


def test_update_connection_toggles_active_and_replaces_permissions():
    user = make_user()
    module = make_installed_module(user)
    session = FakeSession(results=[[module], []])  # lookup then permission delete
    service = AppConnectionService(session)

    updated = asyncio.run(service.update_connection(user, module.id, is_active=False))
    assert updated is module
    assert module.is_active is False


def test_revoke_deletes_module_and_permissions():
    user = make_user()
    module = make_installed_module(user)
    session = FakeSession(results=[[module], []])
    service = AppConnectionService(session)

    assert asyncio.run(service.revoke(user, module.id)) is True
    assert module in session.deleted

    session2 = FakeSession(results=[[]])
    service2 = AppConnectionService(session2)
    assert asyncio.run(service2.revoke(user, module.id)) is False


def test_has_active_permission_true_when_granted():
    user = make_user()
    session = FakeSession(results=[[uuid.uuid4()]])
    service = AppConnectionService(session)
    assert asyncio.run(service.has_active_permission(user, "brain", "brain", "read")) is True


def test_has_active_permission_false_when_absent():
    user = make_user()
    session = FakeSession(results=[[]])
    service = AppConnectionService(session)
    assert asyncio.run(service.has_active_permission(user, "brain", "brain", "read")) is False


def test_connection_endpoints_require_auth():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/v1/apps/me").status_code == 401
        assert client.post("/api/v1/apps/me", json={"module_slug": "meeting"}).status_code == 401
        assert client.delete("/api/v1/apps/me/00000000-0000-0000-0000-000000000000").status_code == 401