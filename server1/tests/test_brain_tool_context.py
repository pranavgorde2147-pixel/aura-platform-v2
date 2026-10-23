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
                "pending_delete_plans": [
                    {
                        "plan_id": str(uuid.uuid4()),
                        "action": "delete_namespace",
                        "namespace": "apps/health",
                        "record_count": 3,
                    }
                ],
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
            "delete_approval_id": str(uuid.uuid4()),
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
    assert result.pending_delete_plans[0]["namespace"] == "apps/health"
    assert inference_context["source_app"] == "diary"
    assert inference_context["authorized_namespaces"] == ["shared"]
    assert inference_context["confirm_tool_writes"] is False
    assert "confirm_tool_deletes" not in inference_context
    assert "confirmed_delete_scope" not in inference_context
    assert "confirmed_delete_count" not in inference_context
    assert inference_context["delete_approval_id"] is None
    assert inference_context["manage_vault"] is False


def test_brain_query_harness_prefix_enables_owner_scoped_management(monkeypatch):
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
                "bulk_replace_result": {
                    "ok": True,
                    "record_count": 2,
                    "occurrence_count": 7,
                },
            }

    monkeypatch.setattr(brain_api, "AIIdentityRepository", FakeIdentityRepository)
    monkeypatch.setattr(brain_api, "PersonalAIBrainRepository", FakeBrainRepository)
    monkeypatch.setattr(brain_api, "AppConnectionService", FakeAppConnectionService)
    monkeypatch.setattr(brain_api, "get_brain_client", FakeBrainClient)

    bulk_plan_id = uuid.uuid4()
    payload = BrainQueryRequest(
        query="harness: Organize my vault",
        source_app="diary",
        use_memory=False,
        manage_vault=False,
        confirm_tool_writes=False,
        bulk_replace_approval_id=bulk_plan_id,
        context={
            "manage_vault": False,
            "harness_mode": False,
            "confirm_tool_writes": False,
            "confirm_tool_deletes": False,
            "confirmed_delete_scope": "namespace:apps/study",
            "confirmed_delete_count": 99,
        },
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
    assert inference_context["harness_mode"] is True
    assert inference_context["confirm_tool_writes"] is True
    assert inference_context["bulk_replace_approval_id"] == str(bulk_plan_id)
    assert result.bulk_replace_result == {
        "ok": True,
        "record_count": 2,
        "occurrence_count": 7,
    }
    assert "confirm_tool_deletes" not in inference_context
    assert "confirmed_delete_scope" not in inference_context
    assert "confirmed_delete_count" not in inference_context
    assert inference_context["delete_approval_id"] is None
    assert inference_context["authorized_namespaces"] == [
        "apps/diary",
        "apps/files",
        "apps/study",
        "conversations",
        "documents",
        "shared",
    ]
