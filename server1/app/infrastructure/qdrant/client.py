"""Qdrant client configuration and connection helpers.

In production the backend talks to a remote Qdrant instance via ``QDRANT_URL``.
For local development and tests, setting ``QDRANT_LOCAL_PATH`` switches to an
embedded, on-disk Qdrant instance so no external service is required.
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from app.core.config import get_settings


class QdrantClientWrapper:
    """Thin wrapper around the Qdrant client for infrastructure use."""

    def __init__(self) -> None:
        self._client: QdrantClient | None = None

    def connect(self) -> QdrantClient:
        """Create and cache a Qdrant client instance."""
        if self._client is None:
            settings = get_settings()
            if settings.qdrant_local_path:
                self._client = QdrantClient(path=settings.qdrant_local_path)
            else:
                if not settings.qdrant_url:
                    raise ValueError("QDRANT_URL is not configured")
                self._client = QdrantClient(url=settings.qdrant_url, timeout=5)
        return self._client

    def close(self) -> None:
        """Close the Qdrant client connection if it exists."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._client = None


qdrant_client = QdrantClientWrapper()
