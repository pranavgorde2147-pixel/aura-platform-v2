"""llama.cpp runtime adapter: one dedicated GGUF runtime per Brain.

Each Brain gets its own ``llama_cpp.Llama`` instance. The weights are the same
read-only file (memory-mapped, so the pages are shared by the OS), but the
*runtime* - its context, its KV cache, its sampling state and its chat
template application - belongs to exactly one Brain.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeHealth,
    RuntimeProvisioningError,
)

_THINK_PAIR = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_PROMPT_ECHO = re.compile(r"^\s*user query\s*:.*?(?:\n|$)", re.IGNORECASE)


def clean_gguf_output(text: str) -> str:
    """Strip reasoning tags and echoed prompt lines from a GGUF completion.

    Small hybrid checkpoints (Qwen3.5-0.8B) tend to emit thinking blocks and
    re-echo the prompt's trailing "User query:" line even when instructed not
    to. Neither belongs in the API response.
    """
    cleaned = _THINK_PAIR.sub("", text)
    cleaned = _THINK_OPEN.sub("", cleaned)
    cleaned = _PROMPT_ECHO.sub("", cleaned)
    return cleaned.strip()


class LlamaCppRuntime:
    """A dedicated llama.cpp runtime bound to one Brain."""

    isolation = "dedicated_context"

    def __init__(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        model_path: Path,
        context_tokens: int,
        max_context_tokens: int,
        gpu_layers: int | None = None,
        threads: int | None = None,
    ) -> None:
        self.brain_id = brain_id
        self.runtime_id = runtime_id
        self.model_path = model_path
        self.isolation = self.isolation
        self._context = RuntimeContext(
            brain_id=brain_id,
            runtime_id=runtime_id,
            max_context_tokens=max_context_tokens,
        )
        self._model: Any = None
        self._closed = False
        self._gpu_layers = gpu_layers
        self._threads = threads
        self._context_tokens = context_tokens
        self._quantization: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        """Create the llama.cpp runtime. Raises on any failure."""
        if not self.model_path.exists():
            raise RuntimeProvisioningError(
                "model_artifact_missing",
                f"Model artifact is not mounted at {self.model_path.name}",
            )
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise RuntimeProvisioningError(
                "adapter_unavailable",
                "llama-cpp-python is not installed; install server2/requirements-gguf.txt",
            ) from exc

        n_gpu_layers = (
            self._gpu_layers
            if self._gpu_layers is not None
            else int(os.getenv("GGUF_N_GPU_LAYERS", "-1" if _cuda_available() else "0"))
        )
        n_threads = self._threads or max(1, min(8, os.cpu_count() or 1))
        try:
            self._model = Llama(
                model_path=str(self.model_path),
                n_ctx=self._context_tokens,
                n_threads=n_threads,
                n_gpu_layers=n_gpu_layers,
                verbose=False,
            )
        except Exception as exc:
            raise RuntimeProvisioningError(
                "model_load_failed", f"llama.cpp runtime could not be created: {type(exc).__name__}"
            ) from exc

        try:
            quant = str(self.model_path.name).rsplit("-", 1)[-1].split(".")[0]
            if quant and not quant.lower().startswith(("q", "iq", "fp")):
                quant = None
        except Exception:  # pragma: no cover - cosmetic
            quant = None
        self._quantization = quant or "gguf"

    # -- inference ---------------------------------------------------------
    @property
    def context(self) -> RuntimeContext:
        return self._context

    def _sampling_kwargs(self, max_new_tokens: int, temperature: float, top_p: float) -> dict[str, Any]:
        return {
            "max_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "repeat_penalty": float(os.getenv("REPEAT_PENALTY", "1.2")),
            "min_p": float(os.getenv("MIN_P", "0.05")),
        }

    def generate(
        self,
        system: str,
        user: str,
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> str:
        if self._model is None or self._closed:
            raise RuntimeProvisioningError("runtime_not_loaded", "The Brain runtime is not loaded")
        kwargs = self._sampling_kwargs(max_new_tokens, temperature, top_p)
        try:
            result = self._model.create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **kwargs,
            )
            text = result["choices"][0]["message"]["content"]
        except Exception:
            # Checkpoints without a usable chat template fall back to completion.
            prompt = f"{system}\nUser query: {user}"
            result = self._model(prompt, echo=False, **kwargs)
            choices = result.get("choices") if isinstance(result, dict) else None
            text = str(choices[0].get("text", "")) if choices else str(result)
        return clean_gguf_output(str(text))

    def health(self) -> RuntimeHealth:
        if self._closed:
            return RuntimeHealth(status="stopped", detail="Runtime has been released")
        if self._model is None:
            return RuntimeHealth(status="unavailable", detail="Runtime has not been loaded")
        return RuntimeHealth(status="healthy")

    def describe(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "brain_id": str(self.brain_id),
            "provider": "gguf",
            "isolation": self.isolation,
            "model_id": self.model_path.name,
            "quantization": self._quantization or "gguf",
            "context_tokens": self._context_tokens,
            "context": self._context.snapshot(),
        }

    def close(self) -> None:
        self._closed = True
        self._model = None
        self._context.reset()


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # pragma: no cover - torch is optional for storage-only runs
        return False


class LlamaCppRuntimeAdapter:
    """Creates one dedicated llama.cpp runtime per Brain."""

    name = "llama_cpp"
    isolation = "dedicated_context"
    provider = "gguf"

    def __init__(self, model_path: Path, *, context_tokens: int, max_context_tokens: int) -> None:
        self.model_path = model_path
        self.context_tokens = context_tokens
        self.max_context_tokens = max_context_tokens

    def create(self, *, brain_id: uuid.UUID, runtime_id: str) -> BrainRuntimeHandle:
        runtime = LlamaCppRuntime(
            brain_id=brain_id,
            runtime_id=runtime_id,
            model_path=self.model_path,
            context_tokens=self.context_tokens,
            max_context_tokens=self.max_context_tokens,
        )
        runtime.load()
        return runtime
