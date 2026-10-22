"""Simulated runtime adapter.

Used by the test-suite and by deployments that run with
``AURA_SKIP_MODEL_LOAD=1``. It creates a *real, dedicated runtime object* per
Brain - with its own identifier, context and cache - but the text it produces
is deterministic and is never presented as model output.

Every response produced through this adapter is tagged ``simulated`` so that
no caller can mistake it for inference.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeHealth,
    RuntimeProvisioningError,
)


class SimulatedRuntime:
    """A dedicated, deterministic runtime used for tests and simulated mode."""

    isolation = "simulated"

    def __init__(self, *, brain_id: uuid.UUID, runtime_id: str, max_context_tokens: int) -> None:
        self.brain_id = brain_id
        self.runtime_id = runtime_id
        self.isolation = self.isolation
        self._context = RuntimeContext(
            brain_id=brain_id, runtime_id=runtime_id, max_context_tokens=max_context_tokens
        )
        self._closed = False
        self.request_count = 0

    @property
    def context(self) -> RuntimeContext:
        return self._context

    def generate(
        self,
        system: str,
        user: str,
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> str:
        if self._closed:
            raise RuntimeProvisioningError("runtime_not_loaded", "The Brain runtime is stopped")
        self.request_count += 1
        digest = hashlib.sha256(
            f"{self.runtime_id}|{self.brain_id}|{user}".encode("utf-8")
        ).hexdigest()
        self._context.append("user", user)
        self._context.append("assistant", digest[:32])
        return f"[simulated:{digest[:16]}]"

    def health(self) -> RuntimeHealth:
        if self._closed:
            return RuntimeHealth(status="stopped", detail="Runtime has been released")
        return RuntimeHealth(status="healthy", detail="simulated")

    def describe(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "brain_id": str(self.brain_id),
            "provider": "simulated",
            "isolation": self.isolation,
            "model_id": "simulated",
            "request_count": self.request_count,
            "context": self._context.snapshot(),
        }

    def close(self) -> None:
        self._closed = True
        self._context.reset()


class SimulatedRuntimeAdapter:
    """Creates one dedicated simulated runtime per Brain."""

    name = "simulated"
    isolation = "simulated"
    provider = "simulated"

    def __init__(self, *, max_context_tokens: int = 8192) -> None:
        self.max_context_tokens = max_context_tokens

    def create(self, *, brain_id: uuid.UUID, runtime_id: str) -> BrainRuntimeHandle:
        return SimulatedRuntime(
            brain_id=brain_id,
            runtime_id=runtime_id,
            max_context_tokens=self.max_context_tokens,
        )
