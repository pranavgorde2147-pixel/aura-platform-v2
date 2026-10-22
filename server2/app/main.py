from __future__ import annotations

import hmac
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from transformers import AutoModelForCausalLM, AutoTokenizer
from app.analysis import (
    build_analysis_prompt,
    deterministic_analysis,
    parse_analysis_response,
)
from app.api.brain_admin import router as brain_admin_router
from app.api.brain_apps import router as brain_apps_router
from app.api.brain_jobs import router as brain_jobs_router
from app.api.brain_vault import router as brain_vault_router
from app.api.brains import get_ready_brain, record_brain_request, router as brains_router
from app.api.objects import router as objects_router
from app.api.storage_health import router as storage_health_router
from app.brain_manager.config import get_brain_manager_config
from app.brain_manager.lifecycle.manager import get_runtime_manager
from app.brain_manager.registry.enums import BrainRuntimeStatus
from app.storage_db import dispose_engine as dispose_storage_engine
from app.storage_db import ensure_schema as ensure_storage_schema
from app.storage_db import get_session_factory as get_storage_session_factory
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

# Object storage endpoints (formerly a separate storage service). Mounted at
# /v1/objects so Server 1's existing StorageClient contract keeps working
# unchanged once its STORAGE_SERVER_URL points at Server 2. Storage health lives
# under /v1/storage to avoid clashing with the brain's own /health and /ready
# probes.
app.include_router(objects_router)
app.include_router(storage_health_router, prefix="/v1/storage")
app.include_router(brains_router)
app.include_router(brain_vault_router)
app.include_router(brain_apps_router)
app.include_router(brain_jobs_router)
app.include_router(brain_admin_router)

_total_inference_slots = asyncio.Semaphore(
    max(1, int(os.getenv("BRAIN_MAX_CONCURRENT_REQUESTS", "4")))
)

#: Runtime statuses that mean the Brain's own runtime can serve a request.
_SERVABLE_RUNTIME_STATUSES = frozenset(
    {
        BrainRuntimeStatus.READY.value,
        BrainRuntimeStatus.READY_EXTERNAL.value,
        BrainRuntimeStatus.READY_SIMULATED.value,
        BrainRuntimeStatus.DEGRADED.value,
    }
)


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
    authorized_memories = _approved_context_entries(
        safe_context.get("authorized_memories"),
        ("id", "kind", "source", "source_app", "content"),
    )
    authorized_records = _approved_context_entries(
        safe_context.get("authorized_records"),
        ("record_id", "source_app", "record_type", "source_reference", "content"),
    )

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
    if authorized_memories:
        context_lines.append(
            "Retrieved user memories (respect each memory's kind and source): "
            + json.dumps(authorized_memories, ensure_ascii=False, sort_keys=True)
        )
    if authorized_records:
        context_lines.append(
            "Retrieved canonical user records (source app and record reference are authoritative): "
            + json.dumps(authorized_records, ensure_ascii=False, sort_keys=True)
        )
    if safety_constraints:
        context_lines.append(f"Safety constraints: {json.dumps(safety_constraints, ensure_ascii=False, sort_keys=True)}")

    return "\n".join(context_lines), query


def _approved_context_entries(value: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only bounded, server-approved provenance fields for prompt context."""
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value[:25]:
        if not isinstance(item, dict):
            continue
        entry = {key: item[key] for key in keys if key in item}
        content = entry.get("content")
        if isinstance(content, str):
            entry["content"] = content[:4000]
        entries.append(entry)
    return entries


def _require_internal_token(authorization: str | None) -> None:
    """Validate the shared internal bearer token.

    Constant-time comparison avoids leaking the token length/prefix via timing.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    expected_token = _internal_api_token()
    if not expected_token or not hmac.compare_digest(token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid internal API token")


@app.middleware("http")
async def enforce_brain_runtime_isolation(request: Request, call_next):
    """Authorize each inference/analysis call against its registered Brain."""
    if request.url.path not in {"/v1/brain/infer", "/v1/brain/analyze"}:
        return await call_next(request)
    try:
        _require_internal_token(request.headers.get("Authorization"))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    try:
        body = await request.body()
        payload = json.loads(body)
        owner_id = uuid.UUID(str(payload.get("user_id", "")))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return JSONResponse({"detail": "user_id must be a valid UUID"}, status_code=400)

    body_sent = False

    async def replay_body():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = replay_body
    await ensure_storage_schema()
    factory = get_storage_session_factory()
    async with factory() as session:
        brain = await get_ready_brain(session, owner_id)
        if brain is None:
            return JSONResponse(
                {"detail": "A provisioned Brain is required for inference"},
                status_code=503,
            )
        if brain.llm_runtime_status not in _SERVABLE_RUNTIME_STATUSES:
            return JSONResponse(
                {
                    "detail": "The Brain's dedicated runtime is not available",
                    "runtime_status": brain.llm_runtime_status,
                    "error_code": brain.runtime_last_error,
                },
                status_code=503,
            )
        brain_key = str(brain.brain_id)
        concurrency = int(brain.compute_allocation.get("max_concurrent_requests", 1))

    brain_slot = get_runtime_manager().slot(uuid.UUID(brain_key), limit=concurrency)
    acquired_global = False
    acquired_brain = False
    try:
        await asyncio.wait_for(_total_inference_slots.acquire(), timeout=5.0)
        acquired_global = True
        await asyncio.wait_for(brain_slot.acquire(), timeout=5.0)
        acquired_brain = True
    except TimeoutError:
        if acquired_brain:
            brain_slot.release()
        if acquired_global:
            _total_inference_slots.release()
        return JSONResponse(
            {"detail": "Brain inference capacity is temporarily exhausted"},
            status_code=429,
        )

    try:
        response = await call_next(request)
    finally:
        if acquired_brain:
            brain_slot.release()
        if acquired_global:
            _total_inference_slots.release()

    if response.status_code < 500:
        async with factory() as session:
            brain = await get_ready_brain(session, owner_id)
            if brain is not None:
                await record_brain_request(session, brain)
    return response


async def _resolve_brain_runtime(session, brain):
    """Return the Brain's own runtime, (re-)creating it when necessary.

    A Brain whose runtime disappeared - for example after a process restart -
    gets a fresh dedicated runtime. When that is not possible the reason is
    returned so the caller can report it honestly instead of falling back to a
    shared runtime.
    """
    manager = get_runtime_manager()
    runtime = manager.handle(brain.brain_id)
    if runtime is not None and runtime.health().healthy:
        return runtime, None
    result = await manager.provision_runtime(session, brain)
    if result.succeeded:
        return manager.handle(brain.brain_id), None
    return None, result.error_code or result.status.value


def _runtime_provider(runtime: Any) -> str:
    try:
        described = runtime.describe() or {}
    except Exception:  # pragma: no cover - defensive
        described = {}
    provider = described.get("provider")
    if provider:
        return str(provider)
    return getattr(runtime, "provider", "local")


def _response_model_name(runtime: Any) -> str:
    """Return the model that produced the answer."""
    if _runtime_provider(runtime) == "external":
        return _external_model_name()
    return _model_name()


@app.post("/v1/brain/infer")
async def brain_infer(request: Request, authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    """Run one inference request on the caller's own dedicated Brain runtime.

    The request is served by the Brain's *own* runtime: its context, its cache
    and its concurrency limit belong to that Brain alone. When the runtime
    cannot be created the request fails with an explicit reason - the request
    is never served by another Brain's runtime.
    """
    _require_internal_token(authorization)

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
    if not isinstance(context, dict):
        raise HTTPException(status_code=400, detail="context must be an object")
    if request_id is not None and not _is_valid_uuid(request_id):
        raise HTTPException(status_code=400, detail="request_id must be a valid UUID when supplied")

    await ensure_storage_schema()
    factory = get_storage_session_factory()
    async with factory() as session:
        brain = await get_ready_brain(session, uuid.UUID(str(user_id)))
        if brain is None:
            raise HTTPException(
                status_code=503, detail="A provisioned Brain is required for inference"
            )

        runtime, error_code = await _resolve_brain_runtime(session, brain)
        if runtime is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The Brain's dedicated runtime is unavailable"
                    f" ({error_code or 'unknown'})"
                ),
            )

        expected_models = {_model_name()}
        external_model = _external_model_name()
        if _external_api_key() and _external_api_url():
            expected_models.add(external_model)
        if model_name is not None and model_name not in expected_models:
            raise HTTPException(
                status_code=400, detail=f"model must be one of {sorted(expected_models)}"
            )

        system, user_query = _build_messages(query.strip(), context)
        config = get_brain_manager_config()
        start = time.perf_counter()
        try:
            response_text = runtime.generate(
                system,
                user_query,
                max_new_tokens=config.max_new_tokens,
                temperature=config.temperature,
                top_p=config.top_p,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"The Brain runtime could not answer: {type(exc).__name__}",
            ) from exc
        latency_ms = int((time.perf_counter() - start) * 1000)

    provider = _runtime_provider(runtime)
    if not response_text:
        response_text = "I could not produce a response with the approved context provided."

    return {
        "model": _response_model_name(runtime),
        "provider": provider,
        "source": "server2_brain_runtime",
        "response": response_text,
        "user_id": str(user_id),
        "request_id": str(request_id) if request_id is not None else str(uuid.uuid4()),
        "status": "ok",
        "tokens_used": len(response_text.split()),
        "latency_ms": latency_ms,
        "brain_id": str(brain.brain_id),
        "llm_runtime_id": runtime.runtime_id,
        "runtime_isolation": runtime.isolation,
        "model_configuration_id": brain.model_configuration_id,
    }


# ---------------------------------------------------------------------------
# Structured analysis
# ---------------------------------------------------------------------------


def _analysis_response(
    *,
    user_id: str,
    request_id: str,
    provider: str,
    model: str,
    segments: list[dict[str, Any]],
    analysis_mode: str,
    latency_ms: int,
    degraded_reason: str | None = None,
    attempted_provider: str | None = None,
) -> dict[str, Any]:
    """Build the analysis response envelope.

    ``provider`` always names the producer of the returned labels. When the
    model was tried and its answer could not be used, the labels come from the
    deterministic analyser, so the provider is ``"deterministic"`` and the
    attempted provider is reported separately.
    """
    return {
        "model": model,
        "provider": provider,
        "attempted_provider": attempted_provider,
        "analysis_mode": analysis_mode,
        "confidence_kind": "model" if analysis_mode == "llm" else "heuristic",
        "request_id": request_id,
        "user_id": user_id,
        "status": "ok",
        "segments": segments,
        "language": None,
        "latency_ms": latency_ms,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
    }


@app.post("/v1/brain/analyze")
async def brain_analyze(request: Request, authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    """Return a structured understanding of one input.

    The response splits the input into independently meaningful segments and
    labels each with a category, a record type and a confidence value. Routing
    and delivery are *not* performed here: Server 1 resolves destinations
    through the application registry and the permission system.

    When the model is unavailable, when the model output cannot be parsed, or
    in test mode, the deterministic analyser is used and the response is
    explicitly labelled ``analysis_mode="deterministic"`` with
    ``confidence_kind="heuristic"`` so the caller never mistakes a heuristic
    score for a calibrated one.
    """
    _require_internal_token(authorization)

    payload = await request.json()
    user_id = payload.get("user_id")
    content = payload.get("content")
    model_name = payload.get("model")
    request_id = payload.get("request_id")
    source_app = payload.get("source_app")
    language = payload.get("language")
    context = payload.get("context") or {}
    max_segments = payload.get("max_segments") or 12

    if not _is_valid_uuid(user_id):
        raise HTTPException(status_code=400, detail="user_id must be a valid UUID")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content must be a non-empty string")
    if not isinstance(context, dict):
        raise HTTPException(status_code=400, detail="context must be an object")
    if request_id is not None and not _is_valid_uuid(request_id):
        raise HTTPException(status_code=400, detail="request_id must be a valid UUID when supplied")
    try:
        max_segments = max(1, min(int(max_segments), 32))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_segments must be an integer")

    if model_name is not None and model_name != _model_name():
        raise HTTPException(status_code=400, detail=f"model must be exactly {_model_name()}")
    model_name = _model_name()
    resolved_request_id = str(request_id) if request_id is not None else str(uuid.uuid4())

    system, user = build_analysis_prompt(
        content,
        source_app=source_app,
        language=language,
        context=context,
    )

    await ensure_storage_schema()
    factory = get_storage_session_factory()
    async with factory() as session:
        brain = await get_ready_brain(session, uuid.UUID(str(user_id)))
        if brain is None:
            raise HTTPException(
                status_code=503, detail="A provisioned Brain is required for analysis"
            )
        runtime, error_code = await _resolve_brain_runtime(session, brain)
        if runtime is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The Brain's dedicated runtime is unavailable"
                    f" ({error_code or 'unknown'})"
                ),
            )

        provider = _runtime_provider(runtime)
        start = time.perf_counter()
        raw_text: str | None = None
        try:
            raw_text = runtime.generate(
                system,
                user,
                max_new_tokens=int(os.getenv("ANALYSIS_MAX_NEW_TOKENS", "512")),
                temperature=get_brain_manager_config().temperature,
                top_p=get_brain_manager_config().top_p,
            )
        except Exception:
            raw_text = None
        latency_ms = int((time.perf_counter() - start) * 1000)

    segments = parse_analysis_response(raw_text) if raw_text else None
    if segments:
        response = _analysis_response(
            user_id=str(user_id),
            request_id=resolved_request_id,
            provider=provider,
            model=model_name,
            segments=segments,
            analysis_mode="llm",
            latency_ms=latency_ms,
        )
        response["language"] = language
        return response

    # The Brain's runtime could not produce a usable structured answer: fall
    # back to the deterministic analyser and mark the response as degraded. The
    # provider names the producer of the labels; the attempted provider is
    # reported separately so a heuristic answer is never presented as a model
    # answer.
    fallback = deterministic_analysis(content, source_app=source_app, max_segments=max_segments)
    degraded_reason = (
        "model_load_skipped" if provider == "simulated" else "llm_output_unusable"
    )
    response = _analysis_response(
        user_id=str(user_id),
        request_id=resolved_request_id,
        provider="deterministic",
        model=model_name,
        segments=fallback,
        analysis_mode="deterministic",
        latency_ms=latency_ms,
        degraded_reason=degraded_reason,
        attempted_provider=provider,
    )
    response["language"] = language
    return response
