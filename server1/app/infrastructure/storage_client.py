"""HTTP client for Server 1 -> storage API (hosted on Server 2).

Server 1 remains the only component that authenticates end users. It forwards
the *authorized* owner id to the storage API for every operation, which is how
cross-user access is prevented end to end.

The object-storage engine runs inside Server 2. Its endpoints are mounted at
``/v1/storage`` (health/readiness) and ``/v1/objects`` (objects);
``STORAGE_SERVER_URL`` points at that Server 2 origin. Misconfigured URLs
(missing/empty or without an ``http(s)://`` scheme) are reported with a clear,
credential-safe message instead of failing inside the HTTP client.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_VALID_SCHEMES = ("http", "https")


class StorageServiceError(Exception):
    """Raised when the storage API returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StorageConfigurationError(StorageServiceError):
    """Raised when ``STORAGE_SERVER_URL`` is missing or malformed."""


def _normalize_base_url(raw_url: str) -> str:
    return (raw_url or "").strip().rstrip("/")


def _validate_base_url(url: str) -> str | None:
    """Return a safe, actionable error message, or ``None`` when valid."""
    if not url:
        return (
            "STORAGE_SERVER_URL is not configured; set it to the Server 2 origin "
            "(for example https://<server2-host>)"
        )
    if urlparse(url).scheme.lower() not in _VALID_SCHEMES:
        return "STORAGE_SERVER_URL must include an explicit scheme (http:// or https://)"
    return None


class StorageClient:
    """Client for the AURA storage API hosted on Server 2."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = _normalize_base_url(settings.storage_server_url)
        self.token = settings.effective_storage_token
        self.timeout = settings.storage_request_timeout_seconds
        self.access_client_id = settings.ai_access_client_id
        self.access_client_secret = settings.ai_access_client_secret
        error = _validate_base_url(self.base_url)
        if error:
            logger.warning("Storage configuration invalid: %s", error)

    def _require_base_url(self) -> str:
        error = _validate_base_url(self.base_url)
        if error:
            raise StorageConfigurationError(error)
        return self.base_url

    def _headers(self, owner_id: str) -> dict[str, str]:
        headers = {"X-Aura-Owner-Id": str(owner_id)}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(self._access_headers())
        return headers

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        headers.update(self._access_headers())
        return headers

    def _access_headers(self) -> dict[str, str]:
        """Add Cloudflare Access service credentials when fully configured."""
        if not self.access_client_id or not self.access_client_secret:
            return {}
        return {
            "CF-Access-Client-Id": self.access_client_id,
            "CF-Access-Client-Secret": self.access_client_secret,
        }

    async def health_check(self) -> dict[str, Any] | None:
        """Return storage readiness JSON, or None if unreachable.

        Raises :class:`StorageConfigurationError` when ``STORAGE_SERVER_URL`` is
        missing or malformed so the cause is reported instead of being hidden.
        """
        base_url = self._require_base_url()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{base_url}/v1/storage/ready",
                    headers=self._auth_headers(),
                )
                if response.status_code == 200:
                    return response.json()
                return {"status": "not_ready", "http_status": response.status_code}
        except httpx.HTTPError as exc:
            logger.warning("Storage health check failed: %s", type(exc).__name__)
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
        base_url = self._require_base_url()
        data: dict[str, str] = {}
        if ai_identity_id:
            data["ai_identity_id"] = str(ai_identity_id)
        if source_app:
            data["source_app"] = source_app

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{base_url}/v1/objects",
                    headers=self._headers(owner_id),
                    files={"file": (filename, content, content_type)},
                    data=data,
                )
        except httpx.HTTPError as exc:
            logger.error("Storage upload failed: %s", type(exc).__name__)
            raise StorageServiceError("Storage service unavailable") from exc

        if response.status_code >= 400:
            raise StorageServiceError(
                f"Storage service rejected upload: {response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json()

    async def list_objects(self, *, owner_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List objects owned by a user."""
        base_url = self._require_base_url()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{base_url}/v1/objects",
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
        base_url = self._require_base_url()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{base_url}/v1/objects/{object_id}/metadata",
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
        base_url = self._require_base_url()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.delete(
                    f"{base_url}/v1/objects/{object_id}",
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
        base_url = self._require_base_url()
        client = httpx.AsyncClient(timeout=self.timeout)
        request = client.build_request(
            "GET",
            f"{base_url}/v1/objects/{object_id}",
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

    async def read_text_object(
        self,
        *,
        owner_id: str,
        object_id: str,
        max_bytes: int = 200_000,
    ) -> str:
        """Read a bounded UTF-8 text object for authorized Brain retrieval."""
        headers, stream = await self.stream_object(owner_id=owner_id, object_id=object_id)
        if not headers["Content-Type"].lower().startswith("text/"):
            async for _ in stream:
                pass
            raise StorageServiceError("The referenced record is not text", status_code=415)
        chunks: list[bytes] = []
        total = 0
        exceeds_limit = False
        async for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                exceeds_limit = True
            elif not exceeds_limit:
                chunks.append(chunk)
        if exceeds_limit:
            raise StorageServiceError(
                "The referenced record exceeds the retrieval size limit",
                status_code=413,
            )
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageServiceError(
                "The referenced record is not valid UTF-8 text",
                status_code=415,
            ) from exc


def get_storage_client() -> StorageClient:
    """Return a StorageClient instance."""
    return StorageClient()
