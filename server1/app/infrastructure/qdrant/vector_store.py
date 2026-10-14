"""Qdrant-backed vector store for Personal Brain memories.

A single collection holds every identity's memory vectors; per-identity
isolation is enforced with a mandatory ``ai_identity_id`` payload filter on
every search. Point IDs are the memory UUIDs from Postgres, so a memory can be
updated or removed in the vector store by ID.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)


class QdrantVectorStore:
    """Small synchronous facade over the Qdrant client used by the indexer."""

    def __init__(self, client: QdrantClient, collection_name: str) -> None:
        self._client = client
        self.collection_name = collection_name

    def ensure_collection(self, dimension: int) -> None:
        """Create the collection if it does not already exist."""
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection %s (dim=%d)", self.collection_name, dimension)

    def upsert(
        self,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Insert or replace a single memory vector."""
        self._client.upsert(
            collection_name=self.collection_name,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    def delete(self, point_id: str) -> None:
        """Remove a memory vector by ID."""
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=[point_id]),
        )

    def search(
        self,
        vector: list[float],
        limit: int,
        ai_identity_id: str,
    ) -> list[tuple[str, float]]:
        """Return ``(point_id, score)`` pairs scoped to a single AI identity."""
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=vector,
            limit=limit,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="ai_identity_id",
                        match=MatchValue(value=ai_identity_id),
                    )
                ]
            ),
        )
        return [(str(point.id), float(point.score)) for point in response.points]
