import pytest
import torch
from fastapi.testclient import TestClient

from app.main import app


class _FakeDelta:
    def __init__(self, content: str, reasoning_content: str | None = None):
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, delta: _FakeDelta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content: str, reasoning_content: str | None = None):
        self.choices = [_FakeChoice(_FakeDelta(content, reasoning_content))]


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return [
            _FakeChunk("External ", None),
            _FakeChunk("provider reply.", None),
        ]


class _FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})


def test_groq_external_mode_forwards(monkeypatch):
    # Configure environment for external Groq mode
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("EXTERNAL_AI_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("EXTERNAL_AI_KEY", "fake-key")
    monkeypatch.setenv("EXTERNAL_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")

    # Mock the Groq SDK client to return a streamed reply
    fake_client = _FakeClient()
    monkeypatch.setattr("app.external_proxy._build_client", lambda: fake_client)

    client = TestClient(app)
    payload = {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "query": "Reply in one short sentence explaining what you can do.",
        "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
        "model": "Qwen3.5-0.8B",
    }
    resp = client.post("/v1/brain/infer", json=payload, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["response"] == "External provider reply."
    assert data["model"] == "qwen/qwen3.8-27b"


def test_local_model_mode_with_fake_runtime(monkeypatch):
    # Ensure local model mode (no skip)
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")

    # Create a fake runtime that mimics tokenizer/model behavior
    class FakeTokenizer:
        eos_token_id = 0

        def __call__(self, prompt, return_tensors=None):
            # return a simple tensor with input length 3
            return {"input_ids": torch.tensor([[1, 2, 3]]), "attention_mask": torch.tensor([[1, 1, 1]])}

        def decode(self, token_slice, skip_special_tokens=True):
            return "Local model reply"

    class FakeModel:
        def generate(self, **kwargs):
            # simulate returning input_ids + one generated token
            import torch as _t

            return _t.tensor([[1, 2, 3, 4]])

    class FakeRuntime:
        def __init__(self):
            self.ready = True
            self.device = "cpu"
            self.tokenizer = FakeTokenizer()
            self.model = FakeModel()

    # Monkeypatch the internal _get_runtime to return fake runtime
    monkeypatch.setattr("app.main._get_runtime", lambda: FakeRuntime())

    client = TestClient(app)
    payload = {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "query": "Reply in one short sentence explaining what you can do.",
        "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
        "model": "Qwen3.5-0.8B",
    }

    resp = client.post("/v1/brain/infer", json=payload, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["response"] == "Local model reply"
    assert data["model"] == "Qwen3.5-0.8B"
