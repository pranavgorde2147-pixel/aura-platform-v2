import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import DEFAULT_GGUF_FILENAME, app, _is_gguf_path


def _candidate_model_path() -> Path | None:
    """Return an existing local checkpoint to test against, if any."""
    configured = os.getenv("MODEL_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path("/models") / DEFAULT_GGUF_FILENAME,
        Path(__file__).resolve().parents[2] / "models" / DEFAULT_GGUF_FILENAME,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return None


def _max_repeated_run(text: str) -> int:
    """Longest run of identical consecutive whitespace tokens."""
    tokens = text.split()
    longest = current = 1
    for previous, token in zip(tokens, tokens[1:]):
        current = current + 1 if token == previous else 1
        longest = max(longest, current)
    return longest if tokens else 0


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("AURA_RUN_INTEGRATION") != "1" or _candidate_model_path() is None,
    reason="requires a local Qwen3.5 checkpoint and AURA_RUN_INTEGRATION=1",
)
def test_real_qwen_inference_loads_once_and_generates(monkeypatch):
    model_path = _candidate_model_path()
    assert model_path is not None

    monkeypatch.setenv("INTERNAL_API_TOKEN", "integration-token")
    monkeypatch.delenv("AURA_SKIP_MODEL_LOAD", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)
    monkeypatch.setenv("MODEL_PATH", str(model_path))
    monkeypatch.setenv("MODEL_NAME", "Qwen3.5-0.8B")
    monkeypatch.setenv("MAX_NEW_TOKENS", "64")
    monkeypatch.setenv("TEMPERATURE", "0.2")
    monkeypatch.setenv("TOP_P", "0.9")
    monkeypatch.setenv("REPEAT_PENALTY", "1.2")

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200, response.text
    health = response.json()
    assert health["model"] == "Qwen3.5-0.8B"
    expected_provider = "gguf" if _is_gguf_path(model_path) else "local"
    assert health["provider"] == expected_provider

    req = {
        "user_id": str(uuid.uuid4()),
        "query": "Answer in exactly one short sentence: what is 2 + 2?",
        "context": {
            "user_level": "general",
            "relevant_context": {"topic": "basic arithmetic"},
            "safety_constraints": [],
        },
        "request_id": str(uuid.uuid4()),
        "model": "Qwen3.5-0.8B",
    }
    auth = {"Authorization": "Bearer integration-token"}
    response = client.post("/v1/brain/infer", json=req, headers=auth)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model"] == "Qwen3.5-0.8B"
    assert payload["status"] == "ok"
    assert payload["user_id"] == req["user_id"]
    assert payload["request_id"] == req["request_id"]
    assert isinstance(payload["response"], str) and payload["response"]
    assert isinstance(payload["tokens_used"], int) and payload["tokens_used"] > 0
    assert isinstance(payload["latency_ms"], int) and payload["latency_ms"] >= 0

    # Guard against the small-model echo/repeat loops this runtime is tuned for.
    assert _max_repeated_run(payload["response"]) < 6, payload["response"]
    assert len(set(payload["response"].split())) > 3, payload["response"]
