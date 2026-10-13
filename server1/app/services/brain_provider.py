"""Provider abstractions for the Personal Brain and future external services."""

from __future__ import annotations

from typing import Any, Protocol

from app.models.user import User


class PersonalBrainLLMProvider(Protocol):
    """Contract for the Personal Brain intelligence provider."""

    async def process_query(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Process a natural-language query using the user's brain context."""

    async def classify_information(
        self,
        user: User,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify or route incoming application data."""

    async def prepare_external_context(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the minimal context package to send to a future external/general LLM."""

    async def personalize_response(
        self,
        user: User,
        base_answer: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Personalize a general answer using the user's private context."""


class GeneralLLMProvider(Protocol):
    """Future abstraction for the external generalized LLM server (Server 2)."""

    async def prepare_context(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return only the context explicitly approved for general-knowledge processing."""

    async def generate_response(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Generate a generalized answer from a minimal context package."""


class PersonalVaultProvider(Protocol):
    """Future abstraction for the personal vault/server (Server 3) storage boundary."""

    async def get_context_for_user(self, user: User) -> dict[str, Any]:
        """Return user-specific context that the Brain is entitled to access."""

    async def store_ingestion(self, user: User, payload: dict[str, Any]) -> dict[str, Any]:
        """Store application-derived data through the vault boundary."""


class MockPersonalBrainLLMProvider:
    """Development-only mock provider for Server 1 before the real Tiny LLM is attached."""

    async def process_query(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context or {}
        return {
            "answer": (
                f"Mock personal brain response for {user.email}: "
                f"I understand the request '{query}' and would use the user's context to answer it."
            ),
            "source": "mock_personal_brain_provider",
            "context_used": bool(context),
            "user_id": str(user.id),
        }

    async def classify_information(
        self,
        user: User,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        content = payload.get("content", "")
        content_type = payload.get("content_type", "text")
        return {
            "user_id": str(user.id),
            "content_type": content_type,
            "labels": ["personal_brain_ingest", "mock_classification"],
            "summary": f"Received {content_type} for {user.email}: {str(content)[:120]}",
            "source_app": payload.get("source_app"),
        }

    async def prepare_external_context(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context or {}
        relevant = {key: value for key, value in context.items() if key in {"schedule", "goals", "known_topics", "user_level"}}
        return {
            "user_id": str(user.id),
            "query": query,
            "user_level": context.get("user_level", "general"),
            "relevant_context": relevant,
            "safety_constraints": ["never_forward_full_vault", "only_send_minimal_context"],
        }

    async def personalize_response(
        self,
        user: User,
        base_answer: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        return (
            f"Personalized for {user.email}: {base_answer} "
            f"(context keys: {', '.join(sorted(context.keys())) if context else 'none'})"
        )


class MockGeneralLLMProvider:
    """Minimal development stub for a future Server 2-style general LLM provider."""

    async def prepare_context(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context or {}
        return {
            "user_id": str(user.id),
            "query": query,
            "relevant_context": {k: v for k, v in context.items() if k in {"user_level", "known_topics", "schedule"}},
        }

    async def generate_response(
        self,
        user: User,
        query: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        return (
            f"General LLM response for {user.email}: "
            f"I am answering '{query}' using only the explicitly provided context {sorted(context.keys())}."
        )


class MockPersonalVaultProvider:
    """Minimal development vault abstraction for a future Server 3 personal-data boundary."""

    async def get_context_for_user(self, user: User) -> dict[str, Any]:
        return {
            "user_id": str(user.id),
            "memory": [],
            "knowledge": [],
            "timeline": [],
            "source": "mock_personal_vault_provider",
        }

    async def store_ingestion(self, user: User, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": str(user.id),
            "stored": True,
            "ingestion": payload,
            "source": "mock_personal_vault_provider",
        }
