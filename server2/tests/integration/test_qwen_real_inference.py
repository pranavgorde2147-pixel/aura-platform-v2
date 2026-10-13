import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("AURA_RUN_INTEGRATION") != "1" or not Path("/models/Qwen3.5-4B").exists(),
    reason="requires a mounted Qwen3.5-4B checkpoint and AURA_RUN_INTEGRATION=1",
)
def test_real_qwen_inference_loads_once_and_generates(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "integration-token")
    monkeypatch.delenv("AURA_SKIP_MODEL_LOAD", raising=False)
    monkeypatch.setenv("MODEL_PATH", "/models/Qwen3.5-4B")
    monkeypatch.setenv("MAX_NEW_TOKENS", "64")
    monkeypatch.setenv("TEMPERATURE", "0.2")
    monkeypatch.setenv("TOP_P", "0.9")

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "Qwen3.5-4B"

    req = {
        "user_id": str(uuid.uuid4()),
        "query": "Answer in exactly one short sentence: what is 2 + 2?",
        "context": {
            "user_level": "general",
            "relevant_context": {"topic": "basic arithmetic"},
            "safety_constraints": [],
        },
        "request_id": str(uuid.uuid4()),
        "model": "Qwen3.5-4B",
    }
    auth = {"Authorization": "Bearer integration-token"}
    response = client.post("/v1/brain/infer", json=req, headers=auth)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model"] == "Qwen3.5-4B"
    assert payload["status"] == "ok"
    assert payload["user_id"] == req["user_id"]
    assert payload["request_id"] == req["request_id"]
    assert isinstance(payload["response"], str) and payload["response"]
    assert isinstance(payload["tokens_used"], int) and payload["tokens_used"] > 0
    assert isinstance(payload["latency_ms"], int) and payload["latency_ms"] >= 0
