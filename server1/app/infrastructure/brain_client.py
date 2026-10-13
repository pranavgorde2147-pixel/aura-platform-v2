"""HTTP client for Server 1 -> Server 2 communication."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class BrainClient:
    """Client for communicating with Server 2 Brain inference service."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.ai_server_url.rstrip("/")
        self.model = settings.ai_server_model
        self.token = settings.internal_api_token
        self.timeout = httpx.Timeout(settings.ai_request_timeout_seconds, connect=5.0)

    async def infer(
        self,
        user_id: str,
        query: str,
        context: dict[str, Any],
        request_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send inference request to Server 2 Brain service."""
        import uuid

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

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/v1/brain/infer"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.TimeoutException:
                logger.error("Timeout calling Server 2 Brain service")
                raise
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Server 2 returned error: {e.response.status_code} - {e.response.text}"
                )
                raise
            except Exception as e:
                logger.error(f"Failed to call Server 2 Brain service: {e}")
                raise

    async def health_check(self) -> dict[str, Any] | None:
        """Check Server 2 health status."""
        url = f"{self.base_url}/health"

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(f"Server 2 health check failed: {e}")
                return None


def get_brain_client() -> BrainClient:
    """Get a BrainClient instance."""
    return BrainClient()
