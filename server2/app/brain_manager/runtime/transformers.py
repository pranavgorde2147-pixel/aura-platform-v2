"""Transformers runtime adapter with private model weights and context per Brain."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeHealth,
    RuntimeProvisioningError,
)
from app.brain_manager.runtime.artifacts import model_artifact_for_brain


def build_quantization_config(mode: str | None):
    """Build a transformers BitsAndBytesConfig for the given quant mode."""
    if not mode:
        return None
    try:
        import torch
        from transformers import BitsAndBytesConfig
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeProvisioningError(
            "adapter_unavailable", "transformers and torch are required for this runtime"
        ) from exc

    if mode == "4bit":
        return BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
    if mode == "8bit":
        return BitsAndBytesConfig(load_in_8bit=True)
    return None


class TransformersRuntime:
    """A dedicated transformers runtime bound to one Brain."""

    isolation = "dedicated_runtime_per_brain_weights"

    def __init__(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        model_path: Path,
        quantization: str | None,
        max_context_tokens: int,
    ) -> None:
        self.brain_id = brain_id
        self.runtime_id = runtime_id
        self.model_path = model_path
        self.isolation = self.isolation
        self._context = RuntimeContext(
            brain_id=brain_id, runtime_id=runtime_id, max_context_tokens=max_context_tokens
        )
        self._model: Any = None
        self._tokenizer: Any = None
        self._device = "cpu"
        self._closed = False
        self._quantization = quantization

    def load(self) -> None:
        if not self.model_path.exists():
            raise RuntimeProvisioningError(
                "model_artifact_missing",
                f"Model artifact is not mounted at {self.model_path.name}",
            )
        if not (self.model_path / "config.json").exists():
            raise RuntimeProvisioningError(
                "model_artifact_invalid",
                "Mounted model directory does not contain config.json",
            )
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeProvisioningError(
                "adapter_unavailable", "transformers and torch are required for this runtime"
            ) from exc

        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self.model_path), local_files_only=True
            )
            quant_mode = self._quantization
            if quant_mode and self._device != "cuda":
                # bitsandbytes quantization requires a CUDA device.
                quant_mode = None
            dtype = torch.bfloat16 if hasattr(torch, "bfloat16") else torch.float32
            if quant_mode:
                self._model = AutoModelForCausalLM.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                    low_cpu_mem_usage=True,
                    use_safetensors=True,
                    quantization_config=build_quantization_config(quant_mode),
                    device_map="auto",
                )
            else:
                self._model = AutoModelForCausalLM.from_pretrained(
                    str(self.model_path),
                    local_files_only=True,
                    low_cpu_mem_usage=True,
                    torch_dtype=dtype,
                    use_safetensors=True,
                )
                self._model.to(self._device)
            self._model.eval()
            self._model.config.use_cache = True
        except RuntimeProvisioningError:
            raise
        except Exception as exc:
            raise RuntimeProvisioningError(
                "model_load_failed",
                f"Transformers runtime could not be created: {type(exc).__name__}",
            ) from exc

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
        if self._model is None or self._closed:
            raise RuntimeProvisioningError("runtime_not_loaded", "The Brain runtime is not loaded")
        import torch

        prompt = f"{system}\nUser query: {user}"
        inputs = self._tokenizer(prompt, return_tensors="pt")
        inputs = {key: value.to(self._device) for key, value in inputs.items()}
        with torch.inference_mode():
            generated = self._model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                temperature=temperature,
                top_p=top_p,
                pad_token_id=self._tokenizer.eos_token_id,
                eos_token_id=self._tokenizer.eos_token_id,
            )
        input_tokens = inputs["input_ids"].shape[1]
        decoded = self._tokenizer.decode(
            generated[0][input_tokens:], skip_special_tokens=True
        )
        return decoded.strip()

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
            "provider": "transformers",
            "isolation": self.isolation,
            "model_id": self.model_path.name,
            "quantization": self._quantization or "none",
            "device": self._device,
            "context": self._context.snapshot(),
        }

    def close(self) -> None:
        self._closed = True
        self._model = None
        self._tokenizer = None
        self._context.reset()


class TransformersRuntimeAdapter:
    """Creates one dedicated transformers runtime per Brain."""

    name = "transformers"
    isolation = "dedicated_runtime_per_brain_weights"
    provider = "local"

    def __init__(
        self,
        model_path: Path,
        *,
        quantization: str | None,
        max_context_tokens: int,
    ) -> None:
        self.model_path = model_path
        self.quantization = quantization
        self.max_context_tokens = max_context_tokens

    def create(
        self,
        *,
        brain_id: uuid.UUID,
        runtime_id: str,
        resource_root: str | None = None,
    ) -> BrainRuntimeHandle:
        model_path, brain_has_private_weights = model_artifact_for_brain(
            self.model_path,
            brain_id=brain_id,
            resource_root=resource_root,
        )
        runtime = TransformersRuntime(
            brain_id=brain_id,
            runtime_id=runtime_id,
            model_path=model_path,
            quantization=self.quantization,
            max_context_tokens=self.max_context_tokens,
        )
        runtime.isolation = (
            "dedicated_runtime_per_brain_weights"
            if brain_has_private_weights
            else "dedicated_runtime_shared_weights"
        )
        runtime.load()
        return runtime
