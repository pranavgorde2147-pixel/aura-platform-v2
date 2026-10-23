from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.api.v1 import brain as brain_api
from app.schemas.brain import BrainQueryRequest


def test_brain_query_replaces_client_supplied_tool_authorization(monkeypatch):
    user_id = uuid.uuid4()
    identity = SimpleNamespace(id=uuid.uuid4(), display_name="Test User")
    brain = SimpleNamespace(id=uuid.uuid4(), provisioning_status="ready")
    inference_context = {}
    record_id = uuid.uuid4()

    class FakeIdentityRepository:
        def __init__(self, session):
            pass

        async def get_by_user_id(self, requested_user_id):
            assert requested_user_id == user_id
            return identity

    class FakeBrainRepository:
        def __init__(self, session):
            pass

        async def get_by_ai_identity_id(self, identity_id):
            assert identity_id == identity.id
            return brain

    class FakeAppConnectionService:
        def __init__(self, session):
            pass

        async def has_active_permission(self, user, app_slug, *, scope, action):
            assert user.id == user_id
            assert app_slug == "diary"
            assert (scope, action) == ("brain", "read")
            return True

    class FakeBrainClient:
        async def vault_request(self, **kwargs):
            if kwargs["method"] == "POST":
                return {"record_id": str(record_id), "version": 1}
            return {"record_id": str(record_id), "version": 2}

        async def infer(self, user, query, context, *, request_id):
            inference_context.update(context)
            return {
                "response": "Answer",
                "source": "server2_brain_runtime",
                "model": "test-model",
            }

    monkeypatch.setattr(
        brain_api, "AIIdentityRepository", FakeIdentityRepository
    )
    monkeypatch.setattr(
        brain_api, "PersonalAIBrainRepository", FakeBrainRepository
    )
    monkeypatch.setattr(brain_api, "AppConnectionService", FakeAppConnectionService)
    monkeypatch.setattr(brain_api, "get_brain_client", FakeBrainClient)

    payload = BrainQueryRequest(
        query="Look at my diary",
        source_app="diary",
        use_memory=False,
        context={
            "source_app": "health",
            "authorized_namespaces": ["apps/health"],
            "confirm_tool_writes": True,
        },
    )
    result = asyncio.run(
        brain_api.query_brain(
            payload,
            current_user=SimpleNamespace(id=user_id),
            session=object(),
        )
    )

    assert result.answer == "Answer"
    assert inference_context["source_app"] == "diary"
    assert inference_context["authorized_namespaces"] == ["shared"]
    assert inference_context["confirm_tool_writes"] is False
    assert inference_context["manage_vault"] is False


def test_brain_query_adds_all_owner_namespaces_only_for_manager_mode(monkeypatch):
    user_id = uuid.uuid4()
    identity = SimpleNamespace(id=uuid.uuid4(), display_name="Test User")
    brain = SimpleNamespace(id=uuid.uuid4(), provisioning_status="ready")
    inference_context = {}

    class FakeIdentityRepository:
        def __init__(self, session):
            pass

        async def get_by_user_id(self, requested_user_id):
            return identity

    class FakeBrainRepository:
        def __init__(self, session):
            pass

        async def get_by_ai_identity_id(self, identity_id):
            return brain

    class FakeAppConnectionService:
        def __init__(self, session):
            pass

        async def has_active_permission(self, user, app_slug, *, scope, action):
            assert app_slug == "diary"
            return True

        async def list_connections(self, user):
            assert user.id == user_id
            return [
                SimpleNamespace(module_slug="diary"),
                SimpleNamespace(module_slug="files"),
                SimpleNamespace(module_slug="study", is_active=False),
            ]

    class FakeBrainClient:
        async def vault_request(self, **kwargs):
            return {"record_id": str(uuid.uuid4()), "version": 1}

        async def infer(self, user, query, context, *, request_id):
            inference_context.update(context)
            return {
                "response": "Organized",
                "source": "server2_brain_runtime",
                "model": "test-model",
            }

    monkeypatch.setattr(brain_api, "AIIdentityRepository", FakeIdentityRepository)
    monkeypatch.setattr(brain_api, "PersonalAIBrainRepository", FakeBrainRepository)
    monkeypatch.setattr(brain_api, "AppConnectionService", FakeAppConnectionService)
    monkeypatch.setattr(brain_api, "get_brain_client", FakeBrainClient)

    payload = BrainQueryRequest(
        query="Organize my vault",
        source_app="diary",
        use_memory=False,
        manage_vault=True,
        context={"manage_vault": False},
    )
    result = asyncio.run(
        brain_api.query_brain(
            payload,
            current_user=SimpleNamespace(id=user_id),
            session=object(),
        )
    )

    assert result.answer == "Organized"
    assert inference_context["manage_vault"] is True
    assert inference_context["authorized_namespaces"] == [
        "apps/diary",
        "apps/files",
        "apps/study",
        "conversations",
        "documents",
        "shared",
    ]
