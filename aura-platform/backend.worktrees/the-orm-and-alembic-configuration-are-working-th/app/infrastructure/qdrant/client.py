"""Qdrant client configuration and connection helpers."""

from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.core.config import get_settings


class QdrantClientWrapper:
    """Thin wrapper around the Qdrant client for infrastructure use."""

    def __init__(self) -> None:
        self._client: QdrantClient | None = None

    def connect(self) -> QdrantClient:
        """Create and cache a Qdrant client instance."""
        if self._client is None:
            settings = get_settings()
            if not settings.qdrant_url:
                raise ValueError("QDRANT_URL is not configured")
            self._client = QdrantClient(url=settings.qdrant_url)
        return self._client

    def close(self) -> None:
        """Close the Qdrant client connection if it exists."""
        self._client = None


qdrant_client = QdrantClientWrapper()
