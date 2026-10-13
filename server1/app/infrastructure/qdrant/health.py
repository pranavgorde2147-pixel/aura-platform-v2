"""Qdrant health-check helpers."""

from __future__ import annotations

from qdrant_client import QdrantClient


def check_qdrant_health(client: QdrantClient) -> bool:
    """Verify connectivity to Qdrant by probing its health endpoint."""
    try:
        response = client.get_collections()
        return response is not None
    except Exception:
        return False
