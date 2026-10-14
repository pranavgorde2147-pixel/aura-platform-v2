"""Qdrant client configuration and connection helpers.

In production the backend talks to a remote Qdrant instance via ``QDRANT_URL``.
Qdrant Cloud (and any secured instance) additionally requires ``QDRANT_API_KEY``,
which the client sends as the ``api-key`` header. For local development and
tests, setting ``QDRANT_LOCAL_PATH`` switches to an embedded, on-disk Qdrant
instance so no external service is required.

The API key is never logged; diagnostics only report the presence/absence of
configuration.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from qdrant_client import QdrantClient

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")


def normalize_qdrant_url(raw_url: str) -> str:
    """Return a validated Qdrant URL or raise ``ValueError``.

    Trims whitespace and trailing slashes and requires an explicit ``http`` or
    ``https`` scheme so misconfiguration is reported clearly instead of failing
    later inside the HTTP client. The value itself is never included in errors.
    """
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("QDRANT_URL is not configured")
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            "QDRANT_URL must include an explicit scheme (http:// or https://)"
        )
    return url


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
                url = normalize_qdrant_url(settings.qdrant_url)
                api_key = (settings.qdrant_api_key or "").strip() or None
                if urlparse(url).scheme.lower() == "https" and api_key is None:
                    logger.warning(
                        "Qdrant URL uses https but QDRANT_API_KEY is not set; "
                        "a secured Qdrant Cloud instance will reject the request"
                    )
                self._client = QdrantClient(url=url, api_key=api_key, timeout=5)
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
