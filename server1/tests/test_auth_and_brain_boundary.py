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


class FakeSessionRepo:
    """In-memory stand-in for SessionRepository used by the auth unit tests."""

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, SimpleNamespace] = {}

    async def create(self, **kwargs):
        record = SimpleNamespace(
            user_id=kwargs["user_id"],
            refresh_token_hash=kwargs["refresh_token_hash"],
            expires_at=kwargs["expires_at"],
            is_revoked=kwargs.get("is_revoked", False),
            revoked_at=None,
            user_agent=kwargs.get("user_agent"),
            ip_address=kwargs.get("ip_address"),
            last_used_at=kwargs.get("last_used_at"),
        )
        self.sessions[kwargs["id"]] = record
        return record

    async def get_by_id(self, session_id):
        return self.sessions.get(session_id)

    async def revoke(self, session_id):
        record = self.sessions.get(session_id)
        if record is None:
            return False
        record.is_revoked = True
        return True

    async def revoke_all_for_user(self, user_id, except_session_id=None):
        count = 0
        for session_id, record in self.sessions.items():
            if record.user_id != user_id or session_id == except_session_id:
                continue
            if not record.is_revoked:
                record.is_revoked = True
                count += 1
        return count


def _auth_service(user: User) -> AuthService:
    service = AuthService(SimpleNamespace(commit=AsyncMock()))
    service.user_repo = SimpleNamespace(
        get_by_email=AsyncMock(return_value=user),
        get_by_id=AsyncMock(return_value=user),
    )
    service.session_repo = FakeSessionRepo()
    return service


def test_login_and_refresh_flow_builds_valid_tokens():
    async def _run():
        user = make_user("alice@example.com")
        service = _auth_service(user)

        login_result = await service.login(user.email, "password123")

        assert login_result["token_type"] == "bearer"
        assert "access_token" in login_result
        assert "refresh_token" in login_result
        assert decode_token(login_result["access_token"], token_type="access")["sub"] == str(user.id)

        refreshed = await service.refresh_token(login_result["refresh_token"])
        assert refreshed["token_type"] == "bearer"
        assert refreshed["access_token"]
        assert refreshed["refresh_token"] != login_result["refresh_token"]
        assert decode_token(refreshed["access_token"], token_type="access")["sub"] == str(user.id)

    asyncio.run(_run())


def test_refresh_rotates_session_and_detects_reuse():
    async def _run():
        user = make_user("rotate@example.com")
        service = _auth_service(user)

        login_result = await service.login(user.email, "password123")
        refreshed = await service.refresh_token(login_result["refresh_token"])
        assert refreshed["refresh_token"] != login_result["refresh_token"]

        # Replaying the now-rotated token is reuse: all sessions are revoked.
        try:
            await service.refresh_token(login_result["refresh_token"])
            raise AssertionError("expected reuse to be rejected")
        except ValueError:
            pass

        assert all(record.is_revoked for record in service.session_repo.sessions.values())
        try:
            await service.refresh_token(refreshed["refresh_token"])
            raise AssertionError("expected all sessions to be revoked")
        except ValueError:
            pass

    asyncio.run(_run())


def test_logout_revokes_session():
    async def _run():
        user = make_user("logout@example.com")
        service = _auth_service(user)

        login_result = await service.login(user.email, "password123")
        await service.logout(login_result["refresh_token"])

        assert all(record.is_revoked for record in service.session_repo.sessions.values())
        try:
            await service.refresh_token(login_result["refresh_token"])
            raise AssertionError("expected logout to invalidate the refresh token")
        except ValueError:
            pass

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
        service.memory_repo = SimpleNamespace(
            upsert_for_identity=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        )
        service.knowledge_repo = SimpleNamespace(
            record_nodes=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        )
        service.timeline_repo = SimpleNamespace(
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
