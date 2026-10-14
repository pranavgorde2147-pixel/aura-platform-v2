"""Qdrant health-check helpers.

Probes report only a coarse, credential-safe failure category (for example
``forbidden`` or ``connection_error``). The API key, response bodies, and
collection contents are never included in the returned reason or in logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse


@dataclass(frozen=True)
class QdrantProbe:
    """Result of a Qdrant connectivity probe."""

    ok: bool
    reason: str | None = None


def _classify_status(status_code: int | None) -> str:
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code is None:
        return "http_error"
    return f"http_{status_code}"


def probe_qdrant(client: QdrantClient) -> QdrantProbe:
    """Probe Qdrant and return a credential-safe result."""
    try:
        client.get_collections()
        return QdrantProbe(ok=True)
    except UnexpectedResponse as exc:
        return QdrantProbe(ok=False, reason=_classify_status(exc.status_code))
    except Exception as exc:
        name = type(exc).__name__
        if "Timeout" in name:
            reason = "timeout"
        elif "Connect" in name:
            reason = "connection_error"
        else:
            reason = "error"
        return QdrantProbe(ok=False, reason=reason)


def check_qdrant_health(client: QdrantClient) -> bool:
    """Verify connectivity to Qdrant by probing its health endpoint."""
    return probe_qdrant(client).ok
