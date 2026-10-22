"""Per-Brain runtime adapters.

A *runtime adapter* knows how to create one inference runtime for exactly one
Brain. The adapter is the only place that talks to llama.cpp, transformers or
an external provider; the lifecycle manager only knows the adapter interface.

The important property of every adapter is that the runtime it returns is
**owned by a single Brain**: it has its own identifier, its own inference
context (KV cache), its own runtime cache and its own health. Model *weights*
may be a shared read-only artefact, but the runtime is never shared.
"""

from __future__ import annotations

from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeHealth,
    RuntimeProvisioningError,
)
from app.brain_manager.runtime.factory import (
    RuntimeAdapter,
    build_runtime_adapter,
    get_runtime_adapter,
    reset_runtime_adapter,
)

__all__ = [
    "BrainRuntimeHandle",
    "RuntimeAdapter",
    "RuntimeContext",
    "RuntimeHealth",
    "RuntimeProvisioningError",
    "build_runtime_adapter",
    "get_runtime_adapter",
    "reset_runtime_adapter",
]
