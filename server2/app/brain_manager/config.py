"""Environment-driven configuration for the Server 2 Brain manager.

Nothing here is secret and nothing here is a credential: the manager only ever
reads *locations*, *limits* and *modes*. API keys stay in the process
environment and are never written into a manifest or a registry row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: One dedicated local runtime/context and complete model copy per Brain.
RUNTIME_MODE_DEDICATED = "dedicated"
#: A dedicated runtime handle per Brain using an external provider.
#: Reported as ``context_isolated_shared_weights``.
RUNTIME_MODE_EXTERNAL = "external"
#: A dedicated runtime handle backed by the deterministic simulator. Used by
#: the test-suite and by ``AURA_SKIP_MODEL_LOAD`` deployments. Always reported
#: as ``simulated`` so it can never be mistaken for real inference.
RUNTIME_MODE_SIMULATED = "simulated"

VALID_RUNTIME_MODES = frozenset(
    {RUNTIME_MODE_DEDICATED, RUNTIME_MODE_EXTERNAL, RUNTIME_MODE_SIMULATED}
)

ADAPTER_AUTO = "auto"
ADAPTER_LLAMA_CPP = "llama_cpp"
ADAPTER_TRANSFORMERS = "transformers"
ADAPTER_SIMULATED = "simulated"

VALID_ADAPTERS = frozenset(
    {ADAPTER_AUTO, ADAPTER_LLAMA_CPP, ADAPTER_TRANSFORMERS, ADAPTER_SIMULATED}
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


@dataclass(frozen=True)
class BrainManagerConfig:
    """Immutable snapshot of the Brain manager configuration."""

    # -- runtime -----------------------------------------------------------
    runtime_mode: str = RUNTIME_MODE_DEDICATED
    runtime_adapter: str = ADAPTER_AUTO
    model_path: str = ""
    model_name: str = "Qwen3.5-0.8B"
    quantization: str = ""
    gpu_layers: int = 0
    context_tokens: int = 2048
    max_new_tokens: int = 256
    temperature: float = 0.2
    top_p: float = 0.8

    # -- capacity ----------------------------------------------------------
    max_concurrent_runtimes: int = 8
    max_concurrent_requests_per_brain: int = 1
    max_context_tokens_per_brain: int = 8192

    # -- resources ---------------------------------------------------------
    resource_root: str = "./data/brains"
    max_jobs_per_brain: int = 64
    job_max_attempts: int = 3

    # -- behaviour ---------------------------------------------------------
    # There is deliberately no shared-runtime fallback: a Brain is only ever
    # served by its own runtime. When that runtime cannot be created the
    # service reports an honest failure instead of borrowing another Brain's
    # runtime.
    external_ai_url: str = ""
    external_ai_key: str = ""
    external_model: str = ""

    @property
    def resolved_resource_root(self) -> Path:
        configured = Path(self.resource_root).expanduser()
        if configured.is_absolute():
            return configured
        server2_root = Path(__file__).resolve().parents[2]
        return server2_root / configured

    @property
    def is_simulated(self) -> bool:
        return self.runtime_mode == RUNTIME_MODE_SIMULATED

    @property
    def uses_external_provider(self) -> bool:
        return self.runtime_mode == RUNTIME_MODE_EXTERNAL


def get_brain_manager_config() -> BrainManagerConfig:
    """Return the Brain manager configuration read from the environment.

    The configuration is read on every call rather than cached: the values
    decide how a runtime is created, and a deployment (or a test) that
    changes ``AURA_SKIP_MODEL_LOAD`` or ``BRAIN_RUNTIME_MODE`` must be
    reflected immediately.
    """
    skip_model_load = _bool("AURA_SKIP_MODEL_LOAD", False)
    external_configured = bool(
        _str("EXTERNAL_AI_URL", "") and _str("EXTERNAL_AI_KEY", "")
    )
    configured_mode = _str("BRAIN_RUNTIME_MODE", "").lower()
    if configured_mode not in VALID_RUNTIME_MODES:
        # A deployment that cannot load a local model must say so explicitly
        # rather than pretending it has a dedicated local runtime.
        if skip_model_load:
            configured_mode = (
                RUNTIME_MODE_EXTERNAL if external_configured else RUNTIME_MODE_SIMULATED
            )
        else:
            configured_mode = RUNTIME_MODE_DEDICATED

    adapter = _str("BRAIN_RUNTIME_ADAPTER", ADAPTER_AUTO).lower()
    if adapter not in VALID_ADAPTERS:
        adapter = ADAPTER_AUTO

    return BrainManagerConfig(
        runtime_mode=configured_mode,
        runtime_adapter=adapter,
        model_path=_str("MODEL_PATH", ""),
        model_name=_str("MODEL_NAME", "Qwen3.5-0.8B"),
        quantization=_str("QUANTIZATION", ""),
        gpu_layers=_int("GGUF_N_GPU_LAYERS", 0),
        context_tokens=_int("GGUF_N_CTX", 2048, minimum=256),
        max_new_tokens=_int("MAX_NEW_TOKENS", 256, minimum=1),
        temperature=float(_str("TEMPERATURE", "0.2") or 0.2),
        top_p=float(_str("TOP_P", "0.8") or 0.8),
        max_concurrent_runtimes=_int("BRAIN_MAX_CONCURRENT_RUNTIMES", 8, minimum=1),
        max_concurrent_requests_per_brain=_int(
            "BRAIN_MAX_CONCURRENT_REQUESTS_PER_BRAIN", 1, minimum=1
        ),
        max_context_tokens_per_brain=_int("BRAIN_MAX_CONTEXT_TOKENS", 8192, minimum=256),
        resource_root=_str("BRAIN_RESOURCE_ROOT", "./data/brains"),
        max_jobs_per_brain=_int("BRAIN_MAX_JOBS_PER_BRAIN", 64, minimum=1),
        job_max_attempts=_int("BRAIN_JOB_MAX_ATTEMPTS", 3, minimum=1),
        external_ai_url=_str("EXTERNAL_AI_URL", ""),
        external_ai_key=_str("EXTERNAL_AI_KEY", ""),
        external_model=_str("EXTERNAL_MODEL", ""),
    )


def reset_brain_manager_config() -> None:
    """Clear the cached runtime adapter (used by the test-suite)."""
    from app.brain_manager.runtime.factory import reset_runtime_adapter

    reset_runtime_adapter()
