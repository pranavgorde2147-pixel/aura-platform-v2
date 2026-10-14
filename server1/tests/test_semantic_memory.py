"""Tests for the semantic memory pipeline (embeddings + Qdrant index).

The vector store runs in embedded/on-disk mode so the full index/search path is
exercised offline. Per-identity isolation is the key security property under
test: a search can never return another identity's memory.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from qdrant_client import QdrantClient

from app.infrastructure.embeddings import HashingEmbeddingProvider
from app.infrastructure.qdrant.vector_store import QdrantVectorStore
from app.models.brain_memory import BrainMemory, MemoryKind, MemorySource
from app.models.user import User
from app.services.memory_index import MemoryIndexer
from app.services.personal_brain import PersonalBrainService


def test_hashing_embedding_is_deterministic_and_normalized():
    provider = HashingEmbeddingProvider(dimension=64)
    first = provider.embed(["I love hiking in the mountains"])[0]
    second = provider.embed(["I love hiking in the mountains"])[0]

    assert len(first) == 64
    assert first == second
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9
    assert provider.embed([""])[0] == [0.0] * 64


def _indexer(tmp_path) -> MemoryIndexer:
    store = QdrantVectorStore(QdrantClient(path=str(tmp_path)), "brain_memories")
    return MemoryIndexer(HashingEmbeddingProvider(dimension=256), store)


def test_indexer_ranks_relevant_memory_and_isolates_identities(tmp_path):
    async def _run():
        indexer = _indexer(tmp_path)
        identity_a = uuid.uuid4()
        identity_b = uuid.uuid4()
        pizza_id = uuid.uuid4()
        hiking_id = uuid.uuid4()
        secret_id = uuid.uuid4()

        assert await indexer.index_memory(
            memory_id=pizza_id, ai_identity_id=identity_a, content="My favourite food is pizza"
        )
        await indexer.index_memory(
            memory_id=hiking_id, ai_identity_id=identity_a, content="I love hiking in the mountains"
        )
        await indexer.index_memory(
            memory_id=secret_id, ai_identity_id=identity_b, content="A private note for another user"
        )

        hits = await indexer.search(identity_a, "pizza", limit=5)
        hit_ids = [memory_id for memory_id, _ in hits]
        assert pizza_id in hit_ids
        # Isolation: another identity's vector is never returned.
        assert secret_id not in hit_ids
        assert hit_ids[0] == pizza_id

        other = await indexer.search(identity_b, "private note", limit=5)
        assert [memory_id for memory_id, _ in other] == [secret_id]

    asyncio.run(_run())


class _FakeIndexer:
    enabled = True

    def __init__(self, hits: list[tuple[uuid.UUID, float]]) -> None:
        self.hits = hits

    async def search(self, ai_identity_id, query, limit):
        return self.hits

    async def index_memory(self, **_):
        return True

    async def delete(self, *_):
        return None


def _make_memory() -> BrainMemory:
    memory = BrainMemory(
        ai_identity_id=uuid.uuid4(),
        kind=MemoryKind.FACT,
        source=MemorySource.USER,
        content="semantic hit",
        importance=0,
        embedding_status="embedded",
    )
    memory.id = uuid.uuid4()
    return memory


def _service_with_indexer(indexer) -> tuple[PersonalBrainService, uuid.UUID]:
    service = PersonalBrainService(SimpleNamespace(), llm_provider=SimpleNamespace())
    identity_id = uuid.uuid4()
    service.identity_repo = SimpleNamespace(
        get_by_user_id=AsyncMock(return_value=SimpleNamespace(id=identity_id))
    )
    service.memory_indexer = indexer
    return service, identity_id


def test_search_uses_vector_index_when_available():
    async def _run():
        memory = _make_memory()
        service, identity_id = _service_with_indexer(_FakeIndexer([(memory.id, 0.99)]))
        service.brain_memory_repo = SimpleNamespace(
            get_by_ids=AsyncMock(return_value=[memory]),
            search_by_identity=AsyncMock(return_value=[]),
        )
        user = User(email="a@example.com", password_hash="x", is_active=True)
        user.id = uuid.uuid4()

        memories = await service.search_memories_for_user(user, "query")

        assert memories == [memory]
        service.brain_memory_repo.get_by_ids.assert_awaited_once()
        service.brain_memory_repo.search_by_identity.assert_not_awaited()

    asyncio.run(_run())


def test_search_falls_back_to_keyword_when_index_is_empty():
    async def _run():
        memory = _make_memory()
        service, _ = _service_with_indexer(_FakeIndexer([]))
        service.brain_memory_repo = SimpleNamespace(
            get_by_ids=AsyncMock(return_value=[]),
            search_by_identity=AsyncMock(return_value=[memory]),
        )
        user = User(email="a@example.com", password_hash="x", is_active=True)
        user.id = uuid.uuid4()

        memories = await service.search_memories_for_user(user, "query")

        assert memories == [memory]
        service.brain_memory_repo.search_by_identity.assert_awaited_once()

    asyncio.run(_run())
