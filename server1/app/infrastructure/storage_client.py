"""HTTP client for Server 1 -> Server 3 (storage) communication.

Server 1 remains the only component that authenticates end users. It forwards
the *authorized* owner id to Server 3 for every operation, which is how
cross-user access is prevented end to end.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class StorageServiceError(Exception):
    """Raised when Server 3 returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StorageClient:
    """Client for the AURA storage engine (Server 3)."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.storage_server_url.rstrip("/")
        self.token = settings.effective_storage_token
        self.timeout = settings.storage_request_timeout_seconds

    def _headers(self, owner_id: str) -> dict[str, str]:
        headers = {"X-Aura-Owner-Id": str(owner_id)}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def health_check(self) -> dict[str, Any] | None:
        """Return Server 3 readiness JSON, or None if unreachable."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/ready")
                if response.status_code == 200:
                    return response.json()
                return {"status": "not_ready", "http_status": response.status_code}
        except Exception as exc:  # pragma: no cover - network guard
            logger.warning("Server 3 health check failed: %s", exc)
            return None

    async def upload(
        self,
        *,
        owner_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        ai_identity_id: str | None = None,
        source_app: str | None = None,
    ) -> dict[str, Any]:
        """Upload an object on behalf of an authenticated user."""
        data: dict[str, str] = {}
        if ai_identity_id:
            data["ai_identity_id"] = str(ai_identity_id)
        if source_app:
            data["source_app"] = source_app

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/objects",
                    headers=self._headers(owner_id),
                    files={"file": (filename, content, content_type)},
                    data=data,
                )
        except httpx.HTTPError as exc:
            logger.error("Storage upload failed: %s", exc)
            raise StorageServiceError("Storage service unavailable") from exc

        if response.status_code >= 400:
            raise StorageServiceError(
                f"Storage service rejected upload: {response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json()

    async def list_objects(self, *, owner_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List objects owned by a user."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/v1/objects",
                    headers=self._headers(owner_id),
                    params={"limit": limit, "offset": offset},
                )
        except httpx.HTTPError as exc:
            raise StorageServiceError("Storage service unavailable") from exc

        if response.status_code >= 400:
            raise StorageServiceError("Storage service rejected listing", status_code=response.status_code)
        return response.json()

    async def get_metadata(self, *, owner_id: str, object_id: str) -> dict[str, Any] | None:
        """Return metadata for an object, or None when not found."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/v1/objects/{object_id}/metadata",
                    headers=self._headers(owner_id),
                )
        except httpx.HTTPError as exc:
            raise StorageServiceError("Storage service unavailable") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StorageServiceError("Storage service rejected metadata request", status_code=response.status_code)
        return response.json()

    async def delete_object(self, *, owner_id: str, object_id: str) -> bool:
        """Delete an object, returning False when it does not exist."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.delete(
                    f"{self.base_url}/v1/objects/{object_id}",
                    headers=self._headers(owner_id),
                )
        except httpx.HTTPError as exc:
            raise StorageServiceError("Storage service unavailable") from exc

        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise StorageServiceError("Storage service rejected delete", status_code=response.status_code)
        return True

    async def stream_object(self, *, owner_id: str, object_id: str) -> tuple[dict[str, str], AsyncIterator[bytes]]:
        """Open a download stream for an object.

        Returns the response headers and an async byte iterator. The caller is
        responsible for fully consuming the iterator so the connection closes.
        """
        client = httpx.AsyncClient(timeout=self.timeout)
        request = client.build_request(
            "GET",
            f"{self.base_url}/v1/objects/{object_id}",
            headers=self._headers(owner_id),
        )
        response = await client.send(request, stream=True)
        if response.status_code == 404:
            await response.aclose()
            await client.aclose()
            raise StorageServiceError("Object not found", status_code=404)
        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise StorageServiceError("Storage service rejected download", status_code=response.status_code)

        headers = {
            "Content-Type": response.headers.get("content-type", "application/octet-stream"),
            "Content-Disposition": response.headers.get("content-disposition", "attachment"),
        }

        async def iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return headers, iterator()


def get_storage_client() -> StorageClient:
    """Return a StorageClient instance."""
    return StorageClient()
