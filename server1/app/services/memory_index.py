"""Semantic memory indexing over Qdrant.

``MemoryIndexer`` embeds memory content and keeps a vector index in sync with
the Postgres ``brain_memories`` table. Every operation degrades gracefully: if
Qdrant or the embedding provider is unavailable, the indexer reports failure and
callers fall back to keyword retrieval instead of failing the request.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.infrastructure.embeddings import EmbeddingProvider, get_embedding_provider
from app.infrastructure.qdrant.client import qdrant_client
from app.infrastructure.qdrant.vector_store import QdrantVectorStore

logger = logging.getLogger(__name__)


class MemoryIndexer:
    """Async wrapper that runs blocking Qdrant/embedding work off the event loop."""

    enabled = True

    def __init__(
        self,
        provider: EmbeddingProvider,
        store: QdrantVectorStore,
    ) -> None:
        self._provider = provider
        self._store = store
        self._ready = False
        self._lock = asyncio.Lock()

    async def _ensure_ready(self) -> bool:
        """Create the collection once; return False if Qdrant is unreachable."""
        if self._ready:
            return True
        async with self._lock:
            if self._ready:
                return True
            try:
                await asyncio.to_thread(
                    self._store.ensure_collection, self._provider.dimension
                )
            except Exception as exc:  # pragma: no cover - depends on environment
                logger.warning("Memory vector index unavailable: %s", type(exc).__name__)
                return False
            self._ready = True
            return True

    async def index_memory(
        self,
        *,
        memory_id: uuid.UUID,
        ai_identity_id: uuid.UUID,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Embed and upsert a memory. Returns True when the index was updated."""
        if not await self._ensure_ready():
            return False
        try:
            vectors = await asyncio.to_thread(self._provider.embed, [content])
            point_payload = {"ai_identity_id": str(ai_identity_id), "content": content}
            if payload:
                point_payload.update(payload)
            await asyncio.to_thread(
                self._store.upsert, str(memory_id), vectors[0], point_payload
            )
            return True
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.warning("Failed to index memory %s: %s", memory_id, type(exc).__name__)
            return False

    async def search(
        self,
        ai_identity_id: uuid.UUID,
        query: str,
        limit: int,
    ) -> list[tuple[uuid.UUID, float]]:
        """Return ``(memory_id, score)`` pairs for one identity."""
        if not query.strip() or not await self._ensure_ready():
            return []
        try:
            vectors = await asyncio.to_thread(self._provider.embed, [query])
            hits = await asyncio.to_thread(
                self._store.search, vectors[0], limit, str(ai_identity_id)
            )
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.warning("Memory vector search failed: %s", type(exc).__name__)
            return []
        results: list[tuple[uuid.UUID, float]] = []
        for point_id, score in hits:
            try:
                results.append((uuid.UUID(point_id), score))
            except ValueError:
                continue
        return results

    async def delete(self, memory_id: uuid.UUID) -> None:
        """Best-effort removal of a memory vector."""
        if not await self._ensure_ready():
            return
        try:
            await asyncio.to_thread(self._store.delete, str(memory_id))
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.warning("Failed to delete memory vector %s: %s", memory_id, type(exc).__name__)


class NoOpMemoryIndexer:
    """Disabled indexer used in tests and when vector search is turned off."""

    enabled = False

    async def index_memory(self, **_: Any) -> bool:
        return False

    async def search(self, *_: Any, **__: Any) -> list[tuple[uuid.UUID, float]]:
        return []

    async def delete(self, *_: Any, **__: Any) -> None:
        return None


@lru_cache(maxsize=1)
def get_memory_indexer() -> MemoryIndexer | NoOpMemoryIndexer:
    """Return the process-wide memory indexer (or a no-op when disabled)."""
    settings = get_settings()
    if not settings.memory_vector_search_enabled:
        return NoOpMemoryIndexer()
    store = QdrantVectorStore(qdrant_client.connect(), settings.qdrant_memory_collection)
    return MemoryIndexer(get_embedding_provider(), store)
