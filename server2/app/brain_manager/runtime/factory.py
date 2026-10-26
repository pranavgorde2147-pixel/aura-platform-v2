"""Runtime adapter factory.

The adapter is chosen once per process from the Brain manager configuration.
The choice is explicit: there is no implicit "share one global model" path.
If the configured adapter cannot be built, provisioning fails loudly with a
stable error code instead of silently degrading to a shared runtime.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.brain_manager.config import (
    ADAPTER_LLAMA_CPP,
    ADAPTER_SIMULATED,
    ADAPTER_TRANSFORMERS,
    BrainManagerConfig,
    RUNTIME_MODE_EXTERNAL,
    RUNTIME_MODE_SIMULATED,
    get_brain_manager_config,
)
from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeHealth,
    RuntimeProvisioningError,
)
@runtime_checkable
class RuntimeAdapter(Protocol):
    """Creates one dedicated runtime per Brain."""

    name: str
    isolation: str
    provider: str

    def create(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        resource_root: str | None = None,
    ) -> BrainRuntimeHandle: ...


def resolve_model_path(config: BrainManagerConfig) -> Path:
    """Resolve the model artifact path from the configuration."""
    configured = config.model_path.strip()
    if configured:
        return Path(configured)
    filename = f"{config.model_name}-Q4_K_M.gguf"
    server2_root = Path(__file__).resolve().parents[3]
    candidates = (
        Path("/models") / filename,
        server2_root / "models" / filename,
        server2_root / filename,
    )
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


def build_runtime_adapter(config: BrainManagerConfig) -> RuntimeAdapter:
    """Build the adapter that matches the configured runtime mode."""
    if config.runtime_mode == RUNTIME_MODE_SIMULATED:
        from app.brain_manager.runtime.simulated import SimulatedRuntimeAdapter

        return SimulatedRuntimeAdapter(max_context_tokens=config.max_context_tokens_per_brain)

    if config.runtime_mode == RUNTIME_MODE_EXTERNAL:
        from app.brain_manager.runtime.external import ExternalProviderRuntimeAdapter

        return ExternalProviderRuntimeAdapter(
            model_id=config.external_model or config.model_name,
            max_context_tokens=config.max_context_tokens_per_brain,
        )

    model_path = resolve_model_path(config)
    adapter_name = config.runtime_adapter

    if adapter_name in {ADAPTER_LLAMA_CPP, "auto"} and model_path.suffix.lower() == ".gguf":
        from app.brain_manager.runtime.llama_cpp import LlamaCppRuntimeAdapter

        return LlamaCppRuntimeAdapter(
            model_path,
            context_tokens=config.context_tokens,
            max_context_tokens=config.max_context_tokens_per_brain,
        )

    if adapter_name in {ADAPTER_TRANSFORMERS, "auto"}:
        from app.brain_manager.runtime.transformers import TransformersRuntimeAdapter

        return TransformersRuntimeAdapter(
            model_path,
            quantization=config.quantization or None,
            max_context_tokens=config.max_context_tokens_per_brain,
        )

    if adapter_name == ADAPTER_SIMULATED:
        from app.brain_manager.runtime.simulated import SimulatedRuntimeAdapter

        return SimulatedRuntimeAdapter(max_context_tokens=config.max_context_tokens_per_brain)

    raise RuntimeProvisioningError(
        "adapter_unavailable", f"Unknown runtime adapter: {config.runtime_adapter}"
    )


_adapter: RuntimeAdapter | None = None
_adapter_key: tuple[str, str, str, str, str] | None = None


def _config_key(config: BrainManagerConfig) -> tuple[str, str, str, str, str]:
    return (
        config.runtime_mode,
        config.runtime_adapter,
        config.model_path,
        config.model_name,
        config.external_model,
    )


def get_runtime_adapter() -> RuntimeAdapter:
    """Return the process-wide adapter instance.

    The adapter is rebuilt when the configuration changes, so a deployment
    that switches ``BRAIN_RUNTIME_MODE`` never keeps serving Brains with the
    previous adapter.
    """
    global _adapter, _adapter_key
    config = get_brain_manager_config()
    key = _config_key(config)
    if _adapter is None or _adapter_key != key:
        _adapter = build_runtime_adapter(config)
        _adapter_key = key
    return _adapter


def reset_runtime_adapter() -> None:
    """Drop the cached adapter (used by the test-suite)."""
    global _adapter, _adapter_key
    _adapter = None
    _adapter_key = None


__all__ = [
    "BrainRuntimeHandle",
    "RuntimeAdapter",
    "RuntimeHealth",
    "RuntimeProvisioningError",
    "build_runtime_adapter",
    "get_runtime_adapter",
    "reset_runtime_adapter",
    "resolve_model_path",
]
