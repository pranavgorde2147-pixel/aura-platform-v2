"""Tests for GGUF model selection, configuration, and health reporting."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import (
    DEFAULT_GGUF_FILENAME,
    DEFAULT_MODEL_NAME,
    GGUFRuntime,
    ModelRuntime,
    _build_runtime,
    _clean_gguf_output,
    _is_gguf_path,
    _model_name,
    _model_path,
)


@pytest.fixture
def clean_model_env(monkeypatch):
    for var in (
        "MODEL_NAME",
        "MODEL_PATH",
        "AURA_SKIP_MODEL_LOAD",
        "EXTERNAL_AI_URL",
        "EXTERNAL_AI_KEY",
        "EXTERNAL_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main_module, "MODEL_RUNTIME", None)


def test_default_model_name_is_local_qwen(clean_model_env):
    assert DEFAULT_MODEL_NAME == "Qwen3.5-0.8B"
    assert _model_name() == "Qwen3.5-0.8B"


def test_model_name_env_overrides_default(clean_model_env, monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "custom-model")
    assert _model_name() == "custom-model"


def test_model_path_honors_explicit_model_path(clean_model_env, monkeypatch, tmp_path):
    gguf = tmp_path / DEFAULT_GGUF_FILENAME
    gguf.write_bytes(b"GGUF")
    monkeypatch.setenv("MODEL_PATH", str(gguf))
    assert _model_path() == gguf


def test_is_gguf_path_distinguishes_file_and_directory(tmp_path):
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    assert _is_gguf_path(gguf) is True
    assert _is_gguf_path(tmp_path) is False
    assert _is_gguf_path(tmp_path / "missing.gguf") is False


def test_build_runtime_selects_gguf_or_transformers(tmp_path):
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF")
    assert isinstance(_build_runtime(gguf), GGUFRuntime)

    model_dir = tmp_path / "hf-checkpoint"
    model_dir.mkdir()
    assert isinstance(_build_runtime(model_dir), ModelRuntime)


def test_health_reports_missing_model_as_unavailable(clean_model_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.setenv("MODEL_NAME", "Qwen3.5-0.8B")
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "missing.gguf"))

    response = TestClient(main_module.app).get("/health")
    assert response.status_code == 503
    assert "Qwen3.5-0.8B" in response.json()["detail"]


def test_health_skip_mode_reports_ok_without_loading(clean_model_env, monkeypatch):
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("MODEL_NAME", "Qwen3.5-0.8B")

    response = TestClient(main_module.app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model"] == "Qwen3.5-0.8B"
    assert body["provider"] == "local"


def test_inference_uses_gguf_runtime(clean_model_env, monkeypatch):
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.setenv("MODEL_NAME", "Qwen3.5-0.8B")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")

    runtime = GGUFRuntime(Path("model.gguf"))
    runtime.ready = True
    runtime.model = object()
    seen = {}

    def fake_generate(system, user, *, max_new_tokens, temperature, top_p):
        seen["system"] = system
        seen["user"] = user
        return "The answer is four."

    monkeypatch.setattr(runtime, "generate", fake_generate)
    monkeypatch.setattr(main_module, "_get_runtime", lambda: runtime)

    payload = {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "query": "What is 2 + 2?",
        "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
        "model": "Qwen3.5-0.8B",
    }
    response = TestClient(main_module.app).post(
        "/v1/brain/infer", json=payload, headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["response"] == "The answer is four."
    assert body["model"] == "Qwen3.5-0.8B"
    assert seen["system"].startswith("You are AURA Server 2")
    assert seen["user"] == "What is 2 + 2?"


def test_clean_gguf_output_strips_thinking_and_prompt_echo():
    raw = "User query: What is 2 + 2?\n\n<think>\nreasoning...\n</think>\n\nThe answer is four."
    assert _clean_gguf_output(raw) == "The answer is four."

    # Unterminated think block is dropped entirely.
    assert _clean_gguf_output("prefix<think>never closed") == "prefix"

    assert _clean_gguf_output("  plain answer  ") == "plain answer"


def test_gguf_generate_uses_chat_template_with_runtime_knobs(monkeypatch, tmp_path):
    monkeypatch.setenv("REPEAT_PENALTY", "1.3")
    monkeypatch.setenv("MIN_P", "0.07")
    captured = {}

    class FakeLlama:
        def create_chat_completion(self, messages, **kwargs):
            captured["messages"] = messages
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "  hello world  "}}]}

    runtime = GGUFRuntime(tmp_path / "model.gguf")
    runtime.model = FakeLlama()
    runtime.ready = True

    text = runtime.generate("system text", "user text", max_new_tokens=8, temperature=0.2, top_p=0.8)
    assert text == "hello world"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "system text"
    assert captured["messages"][1] == {"role": "user", "content": "user text"}
    assert captured["repeat_penalty"] == 1.3
    assert captured["min_p"] == 0.07
    assert captured["max_tokens"] == 8
