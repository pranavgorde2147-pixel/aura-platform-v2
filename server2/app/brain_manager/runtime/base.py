"""Core types shared by every runtime adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


class RuntimeProvisioningError(Exception):
    """Raised when a dedicated runtime cannot be created for a Brain.

    ``code`` is a stable machine-readable reason (``model_artifact_missing``,
    ``model_load_failed``, ``adapter_unavailable``, ``context_budget_exceeded``).
    The message never contains credentials.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RuntimeNotReadyError(RuntimeProvisioningError):
    """Raised when a runtime exists but cannot currently serve a request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass
class RuntimeHealth:
    """The health of one runtime."""

    status: str
    detail: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def healthy(self) -> bool:
        return self.status == "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass
class RuntimeContext:
    """The per-Brain inference context and runtime cache.

    This object is created per Brain and is never shared. It holds the
    conversation turns the runtime has seen and the runtime's own cache, so two
    Brains can never observe each other's context.
    """

    brain_id: uuid.UUID
    runtime_id: str
    max_context_tokens: int = 2048
    messages: list[dict[str, str]] = field(default_factory=list)
    cache: dict[str, Any] = field(default_factory=dict)
    context_tokens_used: int = 0

    def append(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        self.context_tokens_used += max(1, len(content) // 4)

    def reset(self) -> None:
        """Drop the conversation turns and the runtime cache."""
        self.messages.clear()
        self.cache.clear()
        self.context_tokens_used = 0

    def snapshot(self) -> dict[str, Any]:
        """Return a metadata-only snapshot (never the message contents)."""
        return {
            "runtime_id": self.runtime_id,
            "brain_id": str(self.brain_id),
            "message_count": len(self.messages),
            "cache_entries": len(self.cache),
            "context_tokens_used": self.context_tokens_used,
            "max_context_tokens": self.max_context_tokens,
        }


@runtime_checkable
class BrainRuntimeHandle(Protocol):
    """One dedicated inference runtime belonging to exactly one Brain."""

    runtime_id: str
    brain_id: uuid.UUID
    isolation: str

    @property
    def context(self) -> RuntimeContext: ...

    def generate(
        self,
        system: str,
        user: str,
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> str: ...

    def health(self) -> RuntimeHealth: ...

    def describe(self) -> dict[str, Any]:
        """Return runtime metadata. Must never contain credentials."""
        ...

    def close(self) -> None: ...
