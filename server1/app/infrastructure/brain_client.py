"""HTTP client for Server 1 -> Server 2 communication.

Server 1 is the only component that authenticates end users. It forwards the
*authorized* user id to Server 2 for every inference request, which is how
cross-user access is prevented end to end.

Network behaviour is defensive and bounded:

* explicit connect/read timeouts;
* a small, fixed number of retries for transient failures only (connection
  errors and ``429``/``502``/``503``/``504`` responses) with linear backoff,
  never an infinite loop;
* failures are surfaced as :class:`BrainServiceError` carrying a safe message
  and an ``error_code``. Response bodies, credentials, and internal network
  details are never logged or forwarded to clients.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Upstream statuses that are worth retrying: rate limiting and transient
# gateway/unavailability errors. Client errors (4xx other than 429) are not
# retried because they will not succeed on a second attempt.
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


class BrainServiceError(Exception):
    """Raised when Server 2 is unreachable, times out, or rejects a request.

    ``safe_message`` is guaranteed to contain no credentials, response bodies,
    or internal hostnames. ``status_code`` mirrors the upstream HTTP status when
    one was received, otherwise it is ``None``.
    """

    def __init__(
        self,
        error_code: str,
        safe_message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message
        self.status_code = status_code


class BrainClient:
    """Client for communicating with Server 2 Brain inference service."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.ai_server_url.rstrip("/")
        self.model = settings.ai_server_model
        self.token = settings.internal_api_token
        self.timeout = httpx.Timeout(
            settings.ai_request_timeout_seconds,
            connect=settings.ai_connect_timeout_seconds,
        )
        self.max_retries = max(0, settings.ai_max_retries)
        self.retry_backoff = max(0.0, settings.ai_retry_backoff_seconds)
        self.verify_tls = settings.ai_verify_tls
        self.access_client_id = settings.ai_access_client_id
        self.access_client_secret = settings.ai_access_client_secret

    def _headers(self, user_id: str, request_id: str) -> dict[str, str]:
        """Build request headers, never logging their contents."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Aura-Owner-Id": str(user_id),
            "X-Request-Id": str(request_id),
        }
        # Optional edge credentials (for example a Cloudflare Access service
        # token). Sent only when both halves are configured.
        if self.access_client_id and self.access_client_secret:
            headers["CF-Access-Client-Id"] = self.access_client_id
            headers["CF-Access-Client-Secret"] = self.access_client_secret
        return headers

    async def _sleep_backoff(self, attempt: int) -> None:
        """Linear backoff between attempts; bounded by the retry count."""
        if self.retry_backoff > 0:
            await asyncio.sleep(self.retry_backoff * attempt)

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """POST JSON with bounded retries and safe error translation.

        Shared by the inference and analysis calls so both behave identically
        with respect to timeouts, retries and credential-safe errors.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    verify=self.verify_tls,
                ) as client:
                    response = await client.post(url, json=payload, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt <= self.max_retries:
                    logger.warning(
                        "Server 2 brain service connect failure; retrying (attempt %s/%s)",
                        attempt,
                        self.max_retries,
                    )
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(
                    "Server 2 brain service unreachable after %s attempt(s): %s",
                    attempt,
                    type(exc).__name__,
                )
                raise BrainServiceError(
                    "brain_unreachable",
                    "AURA Brain service is unreachable",
                ) from exc
            except httpx.TimeoutException as exc:
                logger.error("Server 2 brain service timed out: %s", type(exc).__name__)
                raise BrainServiceError(
                    "brain_timeout",
                    "AURA Brain service timed out",
                ) from exc
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    logger.warning(
                        "Server 2 brain service transport error; retrying (attempt %s/%s)",
                        attempt,
                        self.max_retries,
                    )
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(
                    "Server 2 brain service transport error after %s attempt(s): %s",
                    attempt,
                    type(exc).__name__,
                )
                raise BrainServiceError(
                    "brain_transport_error",
                    "AURA Brain service is unavailable",
                ) from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                logger.warning(
                    "Server 2 returned transient HTTP %s; retrying (attempt %s/%s)",
                    response.status_code,
                    attempt,
                    self.max_retries,
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                # Never log or forward the response body: it may contain
                # internal implementation details.
                logger.error("Server 2 rejected the request with HTTP %s", response.status_code)
                if response.status_code in (401, 403):
                    raise BrainServiceError(
                        "brain_auth_failed",
                        "AURA Brain rejected the service credentials",
                        response.status_code,
                    )
                raise BrainServiceError(
                    "brain_rejected",
                    "AURA Brain rejected the request",
                    response.status_code,
                )

            try:
                return response.json()
            except ValueError as exc:
                logger.error("Server 2 returned a non-JSON response")
                raise BrainServiceError(
                    "brain_invalid_response",
                    "AURA Brain returned an invalid response",
                ) from exc

    async def infer(
        self,
        user_id: str,
        query: str,
        context: dict[str, Any],
        request_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send an inference request to the Server 2 Brain service.

        Raises :class:`BrainServiceError` on any failure. The caller (the brain
        provider) decides whether to fall back to an external provider.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        if model is None:
            model = self.model

        payload = {
            "user_id": user_id,
            "query": query,
            "model": model,
            "request_id": request_id,
            "context": context,
        }
        headers = self._headers(user_id, request_id)
        url = f"{self.base_url}/v1/brain/infer"
        return await self._post_json(url, payload, headers)

    async def provision_brain(
        self,
        *,
        brain_id: str,
        owner_id: str,
        ai_identity_id: str,
        owner_name: str | None = None,
        model_id: str,
        max_concurrent_requests: int = 1,
    ) -> dict[str, Any]:
        """Register a canonical Server 1 Brain with Server 2 idempotently."""
        payload = {
            "brain_id": brain_id,
            "owner_id": owner_id,
            "ai_identity_id": ai_identity_id,
            "model_id": model_id,
            "resource_manifest_version": 1,
            "max_concurrent_requests": max_concurrent_requests,
        }
        if owner_name is not None:
            payload["owner_name"] = owner_name
        request_id = str(uuid.uuid4())
        headers = self._headers(owner_id, request_id)
        return await self._post_json(
            f"{self.base_url}/v1/brains/provision",
            payload,
            headers,
        )

    async def analyze(
        self,
        *,
        user_id: str,
        content: str,
        context: dict[str, Any] | None = None,
        source_app: str | None = None,
        language: str | None = None,
        request_id: str | None = None,
        model: str | None = None,
        max_segments: int = 12,
    ) -> dict[str, Any]:
        """Ask Server 2 for a structured understanding of one input.

        Returns the decoded analysis envelope. Raises :class:`BrainServiceError`
        when the service is unreachable or rejects the request.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        if model is None:
            model = self.model

        payload: dict[str, Any] = {
            "user_id": user_id,
            "content": content,
            "model": model,
            "request_id": request_id,
            "context": context or {},
            "max_segments": max_segments,
        }
        if source_app is not None:
            payload["source_app"] = source_app
        if language is not None:
            payload["language"] = language

        headers = self._headers(user_id, request_id)
        url = f"{self.base_url}/v1/brain/analyze"
        return await self._post_json(url, payload, headers)

    async def health_check(self) -> dict[str, Any] | None:
        """Check Server 2 health status. Returns ``None`` when unreachable."""
        url = f"{self.base_url}/health"
        headers: dict[str, str] = {}
        if self.access_client_id and self.access_client_secret:
            headers["CF-Access-Client-Id"] = self.access_client_id
            headers["CF-Access-Client-Secret"] = self.access_client_secret

        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                verify=self.verify_tls,
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning("Server 2 health check failed: %s", type(exc).__name__)
            return None


def get_brain_client() -> BrainClient:
    """Get a BrainClient instance."""
    return BrainClient()
