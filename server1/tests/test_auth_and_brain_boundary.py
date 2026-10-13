import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.jwt import decode_token
from app.core.security import hash_password
from app.models.user import User
from app.services.auth import AuthService
from app.services.brain_provider import MockGeneralLLMProvider, MockPersonalBrainLLMProvider
from app.services.personal_brain import PersonalBrainService
from app.services.user import UserService


def make_user(email: str = "user@example.com") -> User:
    user = User(
        email=email,
        password_hash=hash_password("password123"),
        full_name="Test User",
        is_active=True,
    )
    user.id = uuid.uuid4()
    return user


def test_login_and_refresh_flow_builds_valid_tokens():
    async def _run():
        user = make_user("alice@example.com")
        session = SimpleNamespace(commit=AsyncMock())
        service = AuthService(session)
        service.user_repo = SimpleNamespace(
            get_by_email=AsyncMock(return_value=user),
            get_by_id=AsyncMock(return_value=user),
        )

        login_result = await service.login(user.email, "password123")

        assert login_result["token_type"] == "bearer"
        assert "access_token" in login_result
        assert "refresh_token" in login_result
        assert decode_token(login_result["access_token"], token_type="access")["sub"] == str(user.id)

        refreshed = await service.refresh_token(login_result["refresh_token"])
        assert refreshed["token_type"] == "bearer"
        assert refreshed["access_token"]
        assert decode_token(refreshed["access_token"], token_type="access")["sub"] == str(user.id)

    asyncio.run(_run())


def test_personal_brain_uses_provider_and_keeps_user_isolation():
    async def _run():
        user_a = make_user("user-a@example.com")
        user_b = make_user("user-b@example.com")
        provider = MockPersonalBrainLLMProvider()
        service = PersonalBrainService(SimpleNamespace(), llm_provider=provider)
        service.identity_repo = SimpleNamespace(
            get_by_user_id=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
        )
        service.brain_memory_repo = SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
            commit=AsyncMock(),
        )

        result_a = await service.process_query(user_a, "Should I go to the gym tonight?", {"schedule": "busy"})
        result_b = await service.process_query(user_b, "Should I go to the gym tonight?", {"schedule": "free"})

        assert result_a["user_id"] == str(user_a.id)
        assert result_b["user_id"] == str(user_b.id)
        assert user_a.email in result_a["answer"]
        assert user_b.email in result_b["answer"]
        assert user_a.id != user_b.id
        assert "minimal_external_context" in result_a
        assert "user_id" in result_a["minimal_external_context"]

        ingest_result = await service.ingest_data(user_a, {"content": "Today I studied DBMS", "content_type": "text"})
        assert ingest_result["accepted"] is True
        assert ingest_result["stored"] is True
        assert ingest_result["memory_id"] is not None

    asyncio.run(_run())


def test_user_registration_binds_single_identity_and_brain():
    async def _run():
        user = make_user("new-user@example.com")
        session = SimpleNamespace()
        service = UserService(session)
        service.user_repo = SimpleNamespace(create=AsyncMock(return_value=user), commit=AsyncMock())
        service.identity_repo = SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
            commit=AsyncMock(),
            get_by_user_id=AsyncMock(return_value=None),
        )
        service.brain_repo = SimpleNamespace(
            get_by_ai_identity_id=AsyncMock(return_value=None),
            create=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
            commit=AsyncMock(),
        )

        created = await service.create_user("new-user@example.com", hash_password("password123"), "New User")

        assert created.email == "new-user@example.com"
        assert service.identity_repo.create.await_count == 1
        assert service.brain_repo.create.await_count == 1

    asyncio.run(_run())


def test_general_llm_context_is_minimal():
    async def _run():
        user = make_user("general@example.com")
        provider = MockGeneralLLMProvider()
        context = {
            "schedule": "busy",
            "known_topics": ["sql"],
            "full_vault": {"health": "private"},
            "user_level": "intermediate",
        }

        prepared = await provider.prepare_context(user, "Teach me DBMS", context)
        assert prepared["user_id"] == str(user.id)
        assert "full_vault" not in prepared["relevant_context"]
        assert prepared["relevant_context"]["user_level"] == "intermediate"

    asyncio.run(_run())
