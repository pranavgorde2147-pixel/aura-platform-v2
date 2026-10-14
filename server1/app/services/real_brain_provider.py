"""Real Brain LLM provider that calls Server 2 Brain inference service."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.models.user import User

logger = logging.getLogger(__name__)

# Context keys that must never be forwarded to an external provider.
_SENSITIVE_CONTEXT_KEYS = {"vault", "password", "secret", "token", "jwt"}


def _safe_context(context: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (context or {}).items()
        if key not in _SENSITIVE_CONTEXT_KEYS
    }


async def call_external_fallback(query: str, context: dict[str, Any] | None = None) -> str:
    """Call an OpenAI-compatible provider as a fallback for Server 2.

    Only non-sensitive, approved context is sent. Raises on any transport or
    provider error so the caller can decide how to surface the failure.
    """
    settings = get_settings()
    base_url = settings.ai_fallback_url.rstrip("/")
    messages = [
        {
            "role": "system",
            "content": (
                "You are the AURA Personal Brain. Use only the approved context "
                "provided. Do not invent facts or access hidden systems. Answer "
                "directly and concisely."
            ),
        },
        {"role": "user", "content": query},
    ]
    safe = _safe_context(context)
    if safe:
        messages.insert(1, {"role": "system", "content": f"Approved context: {json.dumps(safe, ensure_ascii=False, sort_keys=True)}"})

    async with httpx.AsyncClient(timeout=settings.ai_request_timeout_seconds) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.ai_fallback_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.ai_fallback_model,
                "messages": messages,
                "temperature": 0.2,
            },
        )
        response.raise_for_status()
        data = response.json()
    return data["choices"][0]["message"]["content"]


class RealPersonalBrainLLMProvider:
    """Real implementation of PersonalBrainLLMProvider that calls Server 2."""

    async def _try_external_fallback(
        self,
        query: str,
        context: dict[str, Any] | None,
    ) -> str | None:
        settings = get_settings()
        if not (
            settings.ai_fallback_enabled
            and settings.ai_fallback_url
            and settings.ai_fallback_key
        ):
            return None
        try:
            return await call_external_fallback(query, context)
        except Exception as exc:  # pragma: no cover - logging path
            logger.error(f"External AI fallback also failed: {exc}")
            return None

    async def process_query(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Process a query by calling Server 2 Brain inference service."""
        context = context or {}
        client = get_brain_client()

        try:
            response = await client.infer(
                user_id=str(user.id),
                query=query,
                context=context,
            )
            # Server 2 labels the provider that produced the answer. Test-mode
            # answers must never be presented as real inference.
            provider = response.get("provider")
            source = "server2_brain"
            if provider == "test":
                source = "server2_test_mode"
            return {
                "answer": response.get("response", ""),
                "source": source,
                "provider": provider,
                "context_used": bool(context),
                "user_id": str(user.id),
                "model": response.get("model"),
                "latency_ms": response.get("latency_ms"),
                "tokens_used": response.get("tokens_used"),
            }
        except Exception as e:
            # Log the failure class server-side only. The raw exception string
            # may embed hostnames/URLs and must never reach the client.
            error_code = e.error_code if isinstance(e, BrainServiceError) else "brain_error"
            logger.error("Failed to process query via Server 2 (%s): %s", error_code, type(e).__name__)
            fallback_answer = await self._try_external_fallback(query, context)
            if fallback_answer is not None:
                logger.warning("Server 2 unreachable; answered via external AI fallback")
                return {
                    "answer": fallback_answer,
                    "source": "external_fallback",
                    "context_used": bool(context),
                    "user_id": str(user.id),
                    "model": get_settings().ai_fallback_model,
                    "error_code": error_code,
                }
            return {
                "answer": "The AURA Brain is temporarily unavailable. Please try again shortly.",
                "source": "server2_error",
                "context_used": False,
                "user_id": str(user.id),
                "error_code": error_code,
            }

    async def classify_information(
        self,
        user: User,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify incoming data (currently uses simple local classification)."""
        content = payload.get("content", "")
        content_type = payload.get("content_type", "text")
        return {
            "user_id": str(user.id),
            "content_type": content_type,
            "labels": ["personal_brain_ingest", "classified"],
            "summary": f"Received {content_type} for {user.email}: {str(content)[:120]}",
            "source_app": payload.get("source_app"),
        }

    async def prepare_external_context(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build minimal context for external LLM processing."""
        context = context or {}
        relevant = {
            key: value
            for key, value in context.items()
            if key in {"schedule", "goals", "known_topics", "user_level"}
        }
        return {
            "user_id": str(user.id),
            "query": query,
            "user_level": context.get("user_level", "general"),
            "relevant_context": relevant,
            "safety_constraints": [
                "never_forward_full_vault",
                "only_send_minimal_context",
            ],
        }

    async def personalize_response(
        self,
        user: User,
        base_answer: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Personalize the response with user context."""
        context = context or {}
        context_keys = ", ".join(sorted(context.keys())) if context else "none"
        return f"{base_answer}\n\n[Personalized for {user.email} | Context: {context_keys}]"
