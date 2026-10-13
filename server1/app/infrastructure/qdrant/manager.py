"""Qdrant connection manager for startup and shutdown lifecycle."""

from __future__ import annotations

from qdrant_client import QdrantClient

from app.infrastructure.qdrant.client import qdrant_client


class QdrantManager:
    """Manage Qdrant connection lifecycle for application startup and shutdown."""

    def connect(self) -> QdrantClient:
        """Connect to Qdrant and return the client."""
        return qdrant_client.connect()

    def close(self) -> None:
        """Close the Qdrant client connection."""
        qdrant_client.close()


qdrant_manager = QdrantManager()
