"""External-provider runtime adapter.

When the deployment has no local model (for example a CPU-only host that
delegates to an OpenAI-compatible provider), each Brain still gets its **own**
runtime handle: its own identifier, its own inference context, its own runtime
cache and its own concurrency limit.

What is *not* isolated is the underlying model weights, because they live at
the provider. This mode therefore does not satisfy a requirement for private
per-user local model weights. The runtime reports
``isolation="context_isolated_shared_weights"`` and the registry records the
same value. This is stated explicitly so that no caller believes the Brain has
a private model.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeHealth,
    RuntimeProvisioningError,
)


class ExternalProviderRuntime:
    """A dedicated handle onto a shared external provider."""

    isolation = "context_isolated_shared_weights"

    def __init__(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        model_id: str,
        max_context_tokens: int,
    ) -> None:
        self.brain_id = brain_id
        self.runtime_id = runtime_id
        self.model_id = model_id
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
        from app.external_proxy import call_external_model

        prompt = f"{system}\nUser query: {user}"
        try:
            text = call_external_model(prompt)
        except Exception as exc:
            raise RuntimeProvisioningError(
                "provider_error", f"External provider failed: {type(exc).__name__}"
            ) from exc
        self.request_count += 1
        self._context.append("user", user)
        self._context.append("assistant", text)
        return text

    def health(self) -> RuntimeHealth:
        if self._closed:
            return RuntimeHealth(status="stopped", detail="Runtime has been released")
        return RuntimeHealth(status="healthy", detail="shared external provider")

    def describe(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "brain_id": str(self.brain_id),
            "provider": "external",
            "isolation": self.isolation,
            "model_id": self.model_id,
            "request_count": self.request_count,
            "context": self._context.snapshot(),
        }

    def close(self) -> None:
        self._closed = True
        self._context.reset()


class ExternalProviderRuntimeAdapter:
    """Creates one dedicated external-provider handle per Brain."""

    name = "external"
    isolation = "context_isolated_shared_weights"
    provider = "external"

    def __init__(self, *, model_id: str, max_context_tokens: int = 8192) -> None:
        self.model_id = model_id
        self.max_context_tokens = max_context_tokens

    def create(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        resource_root: str | None = None,
    ) -> BrainRuntimeHandle:
        return ExternalProviderRuntime(
            brain_id=brain_id,
            runtime_id=runtime_id,
            model_id=self.model_id,
            max_context_tokens=self.max_context_tokens,
        )
