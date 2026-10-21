"""Tests for Personal Brain memory persistence.

Memories are explicit and provenance-labelled. These tests assert that facts
and AI interpretations are kept distinct and that nothing is persisted unless
the caller opts in.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.models.brain_memory import BrainMemory, MemoryKind, MemorySource
from app.models.user import User
from app.services.personal_brain import PersonalBrainService


class FakeProvider:
    """Minimal Personal Brain provider that records the context it receives."""

    def __init__(self) -> None:
        self.last_context: dict | None = None

    async def prepare_external_context(self, user, query, context):
        return {"user_id": str(user.id), **context}

    async def process_query(self, user, query, context):
        self.last_context = context
        return {"answer": "mock answer", "source": "fake"}

    async def personalize_response(self, user, answer, context):
        return answer

    async def classify_information(self, user, payload):
        return {"summary": "a summary", "category": "general"}


class FakeVault:
    async def store_ingestion(self, user, payload):
        return {"stored": True}


def make_user(email: str = "user@example.com") -> User:
    user = User(email=email, password_hash="x", full_name="Test", is_active=True)
    user.id = uuid.uuid4()
    return user


def make_identity(identity_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=identity_id or uuid.uuid4())


def make_memory(kind: MemoryKind, content: str) -> BrainMemory:
    memory = BrainMemory(
        ai_identity_id=uuid.uuid4(),
        kind=kind,
        source=MemorySource.USER if kind == MemoryKind.FACT else MemorySource.BRAIN,
        content=content,
        importance=0,
        embedding_status="pending",
    )
    memory.id = uuid.uuid4()
    return memory


def build_service(provider: FakeProvider, identity) -> tuple[PersonalBrainService, SimpleNamespace]:
    session = SimpleNamespace(add=Mock(), flush=AsyncMock())
    service = PersonalBrainService(session, llm_provider=provider, vault_provider=FakeVault())
    memory_repo = SimpleNamespace(
        create=AsyncMock(side_effect=lambda **kwargs: _created_memory(**kwargs)),
        commit=AsyncMock(),
        search_by_identity=AsyncMock(return_value=[]),
        get_recent_by_identity=AsyncMock(return_value=[]),
        get_by_identity=AsyncMock(return_value=[]),
    )
    service.identity_repo = SimpleNamespace(get_by_user_id=AsyncMock(return_value=identity))
    service.brain_memory_repo = memory_repo
    service.app_record_repo = SimpleNamespace(find_by_dedupe=AsyncMock(return_value=None))
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
    return service, memory_repo


def _created_memory(**kwargs) -> BrainMemory:
    memory = BrainMemory(**kwargs, embedding_status="pending")
    memory.id = uuid.uuid4()
    return memory


def test_store_memory_persists_fact_for_identity():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, repo = build_service(provider, identity)

        memory = await service.store_memory_for_user(make_user(), "I use PostgreSQL")

        assert memory is not None
        kwargs = repo.create.await_args.kwargs
        assert kwargs["kind"] == MemoryKind.FACT
        assert kwargs["source"] == MemorySource.USER
        assert kwargs["ai_identity_id"] == identity.id
        repo.commit.assert_awaited_once()

    asyncio.run(_run())


def test_store_memory_returns_none_without_identity():
    async def _run():
        provider = FakeProvider()
        service, repo = build_service(provider, None)

        memory = await service.store_memory_for_user(make_user(), "orphan")

        assert memory is None
        repo.create.assert_not_awaited()

    asyncio.run(_run())


def test_query_without_memory_does_not_read_or_persist():
    async def _run():
        provider = FakeProvider()
        service, repo = build_service(provider, make_identity())

        result = await service.process_query(make_user(), "hello")

        assert result["memories_used"] == []
        assert result["persisted_memory_id"] is None
        repo.search_by_identity.assert_not_awaited()
        repo.create.assert_not_awaited()

    asyncio.run(_run())


def test_query_with_memory_keeps_facts_separate_from_interpretations():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, repo = build_service(provider, identity)
        fact = make_memory(MemoryKind.FACT, "User prefers dark mode")
        interpretation = make_memory(MemoryKind.INTERPRETATION, "User may be a night owl")
        repo.search_by_identity = AsyncMock(return_value=[fact, interpretation])

        result = await service.process_query(make_user(), "What theme?", use_memory=True)

        assert len(result["memories_used"]) == 2
        assert provider.last_context["known_facts"] == ["User prefers dark mode"]
        assert {m["kind"] for m in provider.last_context["relevant_memories"]} == {"fact", "interpretation"}

    asyncio.run(_run())


def test_query_persist_interaction_records_interpretation():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, repo = build_service(provider, identity)

        result = await service.process_query(
            make_user(), "Remember this", persist_interaction=True
        )

        assert result["persisted_memory_id"] is not None
        kwargs = repo.create.await_args.kwargs
        assert kwargs["kind"] == MemoryKind.INTERPRETATION
        assert kwargs["source"] == MemorySource.BRAIN

    asyncio.run(_run())


def test_ingest_persists_fact_memory():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, repo = build_service(provider, identity)

        result = await service.ingest_data(
            make_user(), {"content": "Studied DBMS today", "source_app": "study"}
        )

        assert result["accepted"] is True
        assert result["memory_id"] is not None
        kwargs = repo.create.await_args.kwargs
        assert kwargs["kind"] == MemoryKind.FACT
        # Brain memory keeps the bounded summary, never the full content.
        assert kwargs["content"] == "a summary"
        assert result["record_id"]

    asyncio.run(_run())


def test_ingest_records_vault_knowledge_and_timeline_side_effects():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, _ = build_service(provider, identity)

        result = await service.ingest_data(
            make_user(),
            {
                "content": "Met Alice about the roadmap",
                "object_reference": "objects/note.txt",
                "mime_type": "text/plain",
                "file_size_bytes": 42,
                "source_app": "meeting",
            },
        )

        assert result["vault_entry_id"] is not None
        service.memory_repo.upsert_for_identity.assert_awaited_once()
        service.knowledge_repo.record_nodes.assert_awaited_once()
        service.timeline_repo.create.assert_awaited_once()
        service.timeline_repo.commit.assert_awaited_once()
        assert service.timeline_repo.create.await_args.kwargs["event_type"] == "memory.ingested"

    asyncio.run(_run())


def test_ingest_without_object_reference_skips_vault_write():
    async def _run():
        provider = FakeProvider()
        identity = make_identity()
        service, _ = build_service(provider, identity)

        result = await service.ingest_data(make_user(), {"content": "plain note"})

        assert result["vault_entry_id"] is None
        service.memory_repo.upsert_for_identity.assert_not_awaited()
        service.knowledge_repo.record_nodes.assert_awaited_once()

    asyncio.run(_run())


def test_ingest_stored_reflects_real_write_not_provider_reply():
    async def _run():
        provider = FakeProvider()

        class OptimisticVault:
            """Claims success while persisting nothing (worst-case mock)."""

            async def store_ingestion(self, user, payload):
                return {"stored": True, "source": "mock_personal_vault_provider"}

        service, _ = build_service(provider, make_identity())
        service.vault_provider = OptimisticVault()

        result = await service.ingest_data(make_user(), {"content": "note"})

        assert result["accepted"] is True
        assert result["stored"] is True
        assert result["memory_id"] is not None
        # The provider's optimistic reply must not leak into the response.
        assert "vault_reference" not in result

    asyncio.run(_run())


def test_ingest_stored_false_when_identity_missing():
    async def _run():
        provider = FakeProvider()
        service, _ = build_service(provider, None)

        result = await service.ingest_data(make_user(), {"content": "note"})

        assert result["accepted"] is False
        assert result["stored"] is False

    asyncio.run(_run())


def test_mock_vault_provider_is_marked_development_only():
    from app.services.brain_provider import MockPersonalVaultProvider

    async def _run():
        provider = MockPersonalVaultProvider()
        user = make_user()

        stored = await provider.store_ingestion(user, {"content": "x"})
        assert stored["stored"] is False
        assert stored["source"] == "mock_personal_vault_provider"
        assert stored["development_only"] is True

        context = await provider.get_context_for_user(user)
        assert context["development_only"] is True

    asyncio.run(_run())


def test_brain_query_response_default_source_is_neutral():
    from app.schemas.brain import BrainQueryResponse

    response = BrainQueryResponse(answer="hello")
    assert response.source == "unknown"
    assert "mock" not in response.source


def test_list_memories_empty_without_identity():
    async def _run():
        provider = FakeProvider()
        service, repo = build_service(provider, None)

        memories = await service.list_memories_for_user(make_user())

        assert memories == []
        repo.get_by_identity.assert_not_awaited()

    asyncio.run(_run())
