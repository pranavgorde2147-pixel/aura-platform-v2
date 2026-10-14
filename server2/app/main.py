from __future__ import annotations

import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

import torch
from fastapi import FastAPI, Header, HTTPException, Request
from transformers import AutoModelForCausalLM, AutoTokenizer
from app.external_proxy import call_external_model
from app.api.objects import router as objects_router
from app.api.storage_health import router as storage_health_router
from app.storage_db import dispose_engine as dispose_storage_engine
from app.storage_db import init_models as init_storage_models

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
try:
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    torch.set_num_interop_threads(1)
except RuntimeError:
    pass


def _load_env_file() -> None:
    """Load ``server2/.env`` for local runs.

    Real environment variables always win over the file (``override=False``), so
    Docker/Render/CI settings are never clobbered. Set ``AURA_SKIP_DOTENV=1`` to
    disable loading (the test suite does this so filesystem state stays isolated).
    """
    if os.getenv("AURA_SKIP_DOTENV", "").lower() in {"1", "true", "yes", "on"}:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


_load_env_file()

DEFAULT_MODEL_NAME = "Qwen3.5-0.8B"
DEFAULT_GGUF_FILENAME = "Qwen3.5-0.8B-Q4_K_M.gguf"


def _model_name() -> str:
    return os.getenv("MODEL_NAME", DEFAULT_MODEL_NAME)


def _model_path() -> Path:
    # Resolution order: explicit MODEL_PATH, the in-container mount, then the
    # repo-relative models directory (so local runs need no configuration).
    # Both GGUF files and transformers directories are supported.
    configured = os.getenv("MODEL_PATH")
    repo_models = Path(__file__).resolve().parents[1] / "models"
    candidates = [
        Path(configured) if configured else None,
        Path("/models") / DEFAULT_GGUF_FILENAME,
        Path("/models") / DEFAULT_MODEL_NAME,
        repo_models / DEFAULT_GGUF_FILENAME,
        repo_models / DEFAULT_MODEL_NAME,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    if configured:
        return Path(configured)
    return Path("/models") / DEFAULT_GGUF_FILENAME


def _internal_api_token() -> str:
    return os.getenv("INTERNAL_API_TOKEN", "")


def _skip_model_load() -> bool:
    return os.getenv("AURA_SKIP_MODEL_LOAD", "0").lower() in {"1", "true", "yes", "on"}


def _external_api_key() -> str:
    return os.getenv("EXTERNAL_AI_KEY", "")


def _external_api_url() -> str:
    return os.getenv("EXTERNAL_AI_URL", "")


def _external_model_name() -> str:
    return os.getenv("EXTERNAL_MODEL", "gpt-5-mini")


def _quantization_mode() -> str | None:
    """Return the requested weight quantization mode: "4bit", "8bit", or None.

    Lets a small-VRAM GPU (for example a 6 GB RTX 4050 laptop) run the 4B
    checkpoint in 4-bit instead of OOMing on a full bfloat16 load.
    """
    explicit = os.getenv("QUANTIZATION", "").strip().lower()
    if explicit in {"4bit", "int4", "nf4"}:
        return "4bit"
    if explicit in {"8bit", "int8"}:
        return "8bit"
    if explicit in {"none", "off", "false", "0"}:
        return None
    if os.getenv("LOAD_IN_4BIT", "0").lower() in {"1", "true", "yes", "on"}:
        return "4bit"
    if os.getenv("LOAD_IN_8BIT", "0").lower() in {"1", "true", "yes", "on"}:
        return "8bit"
    return None


def _build_quantization_config(mode: str):
    """Build a transformers BitsAndBytesConfig for the given quant mode."""
    from transformers import BitsAndBytesConfig

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


app = FastAPI(title="AURA Server 2", version="0.1.0")

# Object storage endpoints (formerly Server 3). Mounted at /v1/objects so
# Server 1's existing StorageClient contract keeps working unchanged once its
# STORAGE_SERVER_URL points at Server 2. Storage health lives under /v1/storage
# to avoid clashing with the brain's own /health and /ready probes.
app.include_router(objects_router)
app.include_router(storage_health_router, prefix="/v1/storage")


class ModelRuntime:
    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.tokenizer = None
        self.ready = False
        self.load_error: Exception | None = None
        self.quantization: str | None = None

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model mount missing: {self.model_path}")
        if not (self.model_path / "config.json").exists():
            raise FileNotFoundError(f"Model config missing from mounted path: {self.model_path / 'config.json'}")

        dtype = torch.bfloat16 if (self.device == "cpu" or self.device == "cuda") and hasattr(torch, "bfloat16") else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(str(self.model_path), local_files_only=True)

        quant_mode = _quantization_mode()
        if quant_mode and self.device != "cuda":
            # bitsandbytes quantization requires a CUDA device.
            quant_mode = None

        if quant_mode:
            # BitsAndBytes models must be placed via device_map and cannot be
            # moved afterwards with .to().
            self.model = AutoModelForCausalLM.from_pretrained(
                str(self.model_path),
                local_files_only=True,
                low_cpu_mem_usage=True,
                use_safetensors=True,
                quantization_config=_build_quantization_config(quant_mode),
                device_map="auto",
            )
            self.quantization = quant_mode
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                str(self.model_path),
                local_files_only=True,
                low_cpu_mem_usage=True,
                torch_dtype=dtype,
                use_safetensors=True,
            )
            self.model.to(self.device)
            self.quantization = None

        self.model.eval()
        self.model.config.use_cache = True
        self.ready = True


class GGUFRuntime:
    """Runtime for GGUF checkpoints loaded through llama.cpp.

    llama-cpp-python is an optional dependency (see requirements-gguf.txt). It
    is imported lazily so that environments that only serve transformers
    checkpoints never need it installed.
    """

    provider = "gguf"

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.tokenizer = None
        self.ready = False
        self.load_error: Exception | None = None
        self.quantization: str | None = None

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file missing: {self.model_path}")
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise RuntimeError(
                "llama-cpp-python is not installed; install server2/requirements-gguf.txt to serve GGUF checkpoints"
            ) from exc

        n_gpu_layers = int(os.getenv("GGUF_N_GPU_LAYERS", "-1" if self.device == "cuda" else "0"))
        n_ctx = int(os.getenv("GGUF_N_CTX", "2048"))
        n_threads = max(1, min(8, os.cpu_count() or 1))

        self.model = Llama(
            model_path=str(self.model_path),
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )
        try:
            quant = str(self.model_path.name).rsplit("-", 1)[-1].split(".")[0]
            if quant and not quant.lower().startswith(("q", "iq", "fp")):
                quant = None
        except Exception:  # pragma: no cover - cosmetic
            quant = None
        self.quantization = quant or "gguf"
        self.ready = True

    def _sampling_kwargs(self, max_new_tokens: int, temperature: float, top_p: float) -> dict[str, Any]:
        return {
            "max_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "repeat_penalty": float(os.getenv("REPEAT_PENALTY", "1.2")),
            "min_p": float(os.getenv("MIN_P", "0.05")),
        }

    def generate(self, system: str, user: str, *, max_new_tokens: int, temperature: float, top_p: float) -> str:
        """Generate a reply using the model's chat template.

        Raw completion makes this small hybrid checkpoint echo the prompt and
        loop; the GGUF's embedded chat template applies the right special tokens
        and stop sequences, which keeps output on-topic and terminating.
        """
        if self.model is None or not self.ready:
            raise RuntimeError("GGUF model is not loaded")
        kwargs = self._sampling_kwargs(max_new_tokens, temperature, top_p)
        try:
            result = self.model.create_chat_completion(
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
            result = self.model(prompt, echo=False, **kwargs)
            choices = result.get("choices") if isinstance(result, dict) else None
            text = str(choices[0].get("text", "")) if choices else str(result)
        return _clean_gguf_output(str(text))


_THINK_PAIR = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_PROMPT_ECHO = re.compile(r"^\s*user query\s*:.*?(?:\n|$)", re.IGNORECASE)


def _clean_gguf_output(text: str) -> str:
    """Strip reasoning tags and echoed prompt lines from a GGUF completion.

    Small hybrid checkpoints (Qwen3.5-0.8B) tend to emit `` thinking`` blocks and
    to re-echo the prompt's trailing "User query:" line even when instructed not
    to. Neither belongs in the API response.
    """
    cleaned = _THINK_PAIR.sub("", text)
    cleaned = _THINK_OPEN.sub("", cleaned)
    cleaned = _PROMPT_ECHO.sub("", cleaned)
    return cleaned.strip()


def _is_gguf_path(path: Path) -> bool:
    """A GGUF checkpoint is a single file with a .gguf suffix."""
    return path.is_file() and path.suffix.lower() == ".gguf"


def _build_runtime(path: Path):
    if _is_gguf_path(path):
        return GGUFRuntime(path)
    return ModelRuntime(path)


MODEL_RUNTIME = _build_runtime(_model_path())


def _is_valid_uuid(value: str | None) -> bool:
    if value is None:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _get_runtime():
    path = _model_path()
    global MODEL_RUNTIME
    expected_type = "GGUFRuntime" if _is_gguf_path(path) else "ModelRuntime"
    if MODEL_RUNTIME is None or MODEL_RUNTIME.model_path != path or type(MODEL_RUNTIME).__name__ != expected_type:
        MODEL_RUNTIME = _build_runtime(path)
    if _skip_model_load():
        return MODEL_RUNTIME
    if not MODEL_RUNTIME.ready and MODEL_RUNTIME.load_error is None:
        try:
            MODEL_RUNTIME.load()
        except Exception as exc:  # pragma: no cover - exercised in runtime logs
            MODEL_RUNTIME.load_error = exc
            raise
    return MODEL_RUNTIME


@app.on_event("startup")
def startup_runtime() -> None:
    if _skip_model_load():
        return
    try:
        _get_runtime()
    except Exception as exc:  # pragma: no cover - startup failure is logged and health reports it
        MODEL_RUNTIME.load_error = exc


@app.on_event("startup")
async def startup_storage() -> None:
    """Create storage metadata tables. Failure must not block brain inference."""
    try:
        await init_storage_models()
    except Exception as exc:  # pragma: no cover - runtime guard
        import logging

        logging.getLogger(__name__).warning("Storage metadata init failed: %s", exc)


@app.on_event("shutdown")
async def shutdown_storage() -> None:
    try:
        await dispose_storage_engine()
    except Exception:  # pragma: no cover - shutdown guard
        pass


@app.get("/health")
def health() -> dict[str, str]:
    if _skip_model_load():
        if _external_api_key() and _external_api_url():
            provider_model = _external_model_name()
            return {"status": "ok", "provider": "external", "model": provider_model, "provider_url": _external_api_url()}
        return {"status": "ok", "provider": "local", "model": _model_name()}
    model_name = _model_name()
    try:
        runtime = _get_runtime()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{model_name} model failed to load: {exc}") from exc
    if runtime.ready:
        return {
            "status": "ok",
            "provider": getattr(runtime, "provider", "local"),
            "model": model_name,
            "quantization": runtime.quantization or "none",
        }
    raise HTTPException(status_code=503, detail=f"{model_name} model is unavailable")


@app.get("/ready")
def ready() -> dict[str, bool | str]:
    if _skip_model_load():
        if _external_api_key() and _external_api_url():
            provider_model = _external_model_name()
            return {"status": "ready", "model": provider_model, "provider": "external", "model_loaded": True}
        return {"status": "ready", "model": _model_name(), "provider": "local", "model_loaded": True}
    model_name = _model_name()
    model_ready = False
    provider = "local"
    try:
        runtime = _get_runtime()
        model_ready = runtime.ready
        provider = getattr(runtime, "provider", "local")
    except Exception:
        model_ready = False
    return {"status": "ready" if model_ready else "not_ready", "model": model_name, "provider": provider, "model_loaded": model_ready}


def _build_messages(query: str, context: dict[str, Any]) -> tuple[str, str]:
    """Return (system, user) prompts with sensitive context keys stripped."""
    safe_context = {k: v for k, v in (context or {}).items() if k not in {"vault", "password", "secret", "token", "jwt"}}
    user_level = safe_context.get("user_level")
    relevant_context = safe_context.get("relevant_context", {})
    safety_constraints = safe_context.get("safety_constraints", [])

    if _skip_model_load() and _external_api_key() and _external_api_url():
        model_display = _external_model_name()
    else:
        model_display = _model_name()

    context_lines = [
        f"You are AURA Server 2 running the {model_display} Personal Brain.",
        "Use only the approved context provided by Server 1.",
        "Do not invent facts, fabricate memories, or access hidden systems.",
        "Answer directly, concisely, and safely.",
        "Do NOT include reasoning steps or thinking tags in your output. Give only the final answer.",
    ]
    if user_level:
        context_lines.append(f"User level: {user_level}")
    if relevant_context:
        context_lines.append(f"Relevant context: {json.dumps(relevant_context, ensure_ascii=False, sort_keys=True)}")
    if safety_constraints:
        context_lines.append(f"Safety constraints: {json.dumps(safety_constraints, ensure_ascii=False, sort_keys=True)}")

    return "\n".join(context_lines), query


def _build_prompt(query: str, context: dict[str, Any]) -> str:
    """Flatten the messages into a single completion prompt (transformers/external)."""
    system, user = _build_messages(query, context)
    return f"{system}\nUser query: {user}"


def _generate_test_response() -> str:
    return "This is a test-mode response generated without loading the local Qwen3.5 checkpoint."


# External-hosted LLM proxy helper
def _call_external_model(prompt: str) -> str:
    url = _external_api_url()
    key = _external_api_key()
    model_name = _external_model_name()
    if not url or not key:
        raise RuntimeError("External AI provider not configured (EXTERNAL_AI_URL/EXTERNAL_AI_KEY)")

    payload = {"model": model_name, "input": prompt}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"external provider HTTP error: {exc.code} {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"external provider request failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except Exception:
        # Not JSON — return raw body
        return body.strip()

    # Try common response shapes
    if isinstance(parsed, dict):
        for k in ("response", "output", "text", "result"):
            if k in parsed and isinstance(parsed[k], str):
                return parsed[k].strip()
        if "choices" in parsed and parsed["choices"]:
            ch = parsed["choices"][0]
            if isinstance(ch, dict):
                if "message" in ch and isinstance(ch["message"], dict) and "content" in ch["message"]:
                    return ch["message"]["content"].strip()
                if "text" in ch and isinstance(ch["text"], str):
                    return ch["text"].strip()
    # Fallback to serialized JSON
    return json.dumps(parsed)[:10000]


@app.post("/v1/brain/infer")
async def brain_infer(request: Request, authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    expected_token = _internal_api_token()
    # Constant-time comparison avoids leaking the token length/prefix via timing.
    if not expected_token or not hmac.compare_digest(token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid internal API token")

    payload = await request.json()
    user_id = payload.get("user_id")
    query = payload.get("query")
    model_name = payload.get("model")
    request_id = payload.get("request_id")
    context = payload.get("context") or {}

    if not _is_valid_uuid(user_id):
        raise HTTPException(status_code=400, detail="user_id must be a valid UUID")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query must be a non-empty string")
    if _skip_model_load():
        if _external_api_key() and _external_api_url():
            expected_model = _external_model_name()
            valid_models = {_model_name(), expected_model}
            if model_name not in valid_models:
                raise HTTPException(status_code=400, detail=f"model must be one of {valid_models}")
        else:
            expected_model = _model_name()
            if model_name != expected_model:
                raise HTTPException(status_code=400, detail=f"model must be exactly {expected_model}")
    else:
        expected_model = _model_name()
        if model_name != expected_model:
            raise HTTPException(status_code=400, detail=f"model must be exactly {expected_model}")
    if request_id is not None and not _is_valid_uuid(request_id):
        raise HTTPException(status_code=400, detail="request_id must be a valid UUID when supplied")
    if not isinstance(context, dict):
        raise HTTPException(status_code=400, detail="context must be an object")

    # If we're running in skip mode and an external hosted LLM is configured,
    # route the request to the external OpenAI-compatible provider (Groq) instead
    if _skip_model_load():
        if _external_api_key() and _external_api_url():
            prompt = _build_prompt(query.strip(), context)
            start = time.perf_counter()
            try:
                response_text = call_external_model(prompt)
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"External AI provider error: {exc}") from exc
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "model": _external_model_name(),
                "response": response_text,
                "user_id": str(user_id),
                "request_id": str(request_id) if request_id is not None else str(uuid.uuid4()),
                "status": "ok",
                "tokens_used": len(response_text.split()),
                "latency_ms": latency_ms,
            }

        # fallback test response when skip mode is enabled but no external provider is configured
        latency_ms = 0
        start = time.perf_counter()
        response_text = _generate_test_response()
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "model": _model_name(),
            "response": response_text,
            "user_id": str(user_id),
            "request_id": str(request_id) if request_id is not None else str(uuid.uuid4()),
            "status": "ok",
            "tokens_used": len(response_text.split()),
            "latency_ms": latency_ms,
        }

    try:
        runtime = _get_runtime()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{_model_name()} model is unavailable: {exc}") from exc

    if not runtime.ready or runtime.model is None:
        raise HTTPException(status_code=503, detail=f"{_model_name()} model is unavailable")

    start = time.perf_counter()

    if isinstance(runtime, GGUFRuntime):
        system, user_query = _build_messages(query.strip(), context)
        response_text = runtime.generate(
            system,
            user_query,
            max_new_tokens=int(os.getenv("MAX_NEW_TOKENS", "96")),
            temperature=float(os.getenv("TEMPERATURE", "0.2")),
            top_p=float(os.getenv("TOP_P", "0.8")),
        )
        generated_tokens = len(response_text.split())
    else:
        prompt = _build_prompt(query.strip(), context)
        inputs = runtime.tokenizer(prompt, return_tensors="pt")
        inputs = {k: v.to(runtime.device) for k, v in inputs.items()}

        with torch.inference_mode():
            generated = runtime.model.generate(
                **inputs,
                max_new_tokens=int(os.getenv("MAX_NEW_TOKENS", "96")),
                do_sample=False,
                temperature=float(os.getenv("TEMPERATURE", "0.2")),
                top_p=float(os.getenv("TOP_P", "0.8")),
                pad_token_id=runtime.tokenizer.eos_token_id,
                eos_token_id=runtime.tokenizer.eos_token_id,
            )

        input_tokens = inputs["input_ids"].shape[1]
        generated_tokens = generated.shape[1] - input_tokens
        response_text = runtime.tokenizer.decode(
            generated[0][input_tokens:],
            skip_special_tokens=True,
        ).strip()

    latency_ms = int((time.perf_counter() - start) * 1000)
    if not response_text:
        response_text = "I could not produce a response with the approved context provided."

    return {
        "model": _model_name(),
        "response": response_text,
        "user_id": str(user_id),
        "request_id": str(request_id) if request_id is not None else str(uuid.uuid4()),
        "status": "ok",
        "tokens_used": int(generated_tokens),
        "latency_ms": latency_ms,
    }
