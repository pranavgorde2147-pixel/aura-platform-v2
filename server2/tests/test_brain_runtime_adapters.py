"""Runtime-adapter and lifecycle tests for the per-Brain LLM runtimes.

These tests verify the *actual* adapter behaviour, not just configuration:
every adapter must build a genuinely separate runtime object for every Brain,
the runtime must own its own context, and the lifecycle manager must really
stop, release and replace a runtime.

The real llama.cpp and transformers adapters are exercised with a stub model
class so that the test proves "one model instance per Brain" without needing a
multi-gigabyte checkpoint.
"""

from __future__ import annotations

import uuid

import pytest


def test_simulated_adapter_builds_one_runtime_object_per_brain():
    from app.brain_manager.runtime.simulated import SimulatedRuntimeAdapter

    adapter = SimulatedRuntimeAdapter(max_context_tokens=4096)
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()

    first = adapter.create(brain_id=first_brain, runtime_id="rt_first")
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    assert first is not second
    assert first.brain_id == first_brain
    assert second.brain_id == second_brain
    assert first.context is not second.context
    assert first.context.brain_id == first_brain
    assert second.context.brain_id == second_brain
    assert first.isolation == "simulated"


def test_llama_cpp_adapter_builds_one_llama_instance_per_brain(monkeypatch, tmp_path):
    """The llama.cpp adapter really constructs one ``Llama`` per Brain."""
    llama_cpp = pytest.importorskip("llama_cpp")

    created: list[object] = []

    class _StubLlama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def create_chat_completion(self, **kwargs):
            return {"choices": [{"message": {"content": "stub answer"}}]}

    monkeypatch.setattr(llama_cpp, "Llama", _StubLlama)

    from app.brain_manager.runtime.llama_cpp import LlamaCppRuntimeAdapter

    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"not-a-real-gguf")

    adapter = LlamaCppRuntimeAdapter(
        artifact, context_tokens=2048, max_context_tokens=4096
    )
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()
    first = adapter.create(brain_id=first_brain, runtime_id="rt_first")
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    # Two Brains, two separate llama.cpp runtimes (and therefore two separate
    # KV caches), even though the weight file is the same.
    assert len(created) == 2
    assert created[0] is not created[1]
    assert first.isolation == "dedicated_context"
    assert second.isolation == "dedicated_context"
    assert first.context is not second.context
    assert first.context.brain_id == first_brain
    assert second.context.brain_id == second_brain


def test_llama_cpp_adapter_reports_a_missing_artifact(tmp_path):
    """A missing model artifact fails loudly instead of sharing a runtime."""
    from app.brain_manager.runtime.base import RuntimeProvisioningError
    from app.brain_manager.runtime.llama_cpp import LlamaCppRuntimeAdapter

    adapter = LlamaCppRuntimeAdapter(
        tmp_path / "absent.gguf", context_tokens=2048, max_context_tokens=4096
    )
    with pytest.raises(RuntimeProvisioningError) as exc:
        adapter.create(brain_id=uuid.uuid4(), runtime_id="rt_x")
    assert exc.value.code == "model_artifact_missing"


def test_transformers_adapter_builds_one_model_instance_per_brain(
    monkeypatch, tmp_path
):
    """The transformers adapter really constructs one model per Brain."""
    transformers = pytest.importorskip("transformers")

    created: list[object] = []

    class _StubConfig:
        use_cache = True

    class _StubModel:
        def __init__(self, *args, **kwargs):
            self.config = _StubConfig()
            created.append(self)

        def to(self, device):
            return self

        def eval(self):
            return self

    class _StubTokenizer:
        eos_token_id = 0

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return cls()

    monkeypatch.setattr(
        transformers.AutoModelForCausalLM, "from_pretrained", _StubModel
    )
    monkeypatch.setattr(
        transformers.AutoTokenizer, "from_pretrained", _StubTokenizer.from_pretrained
    )

    from app.brain_manager.runtime.transformers import TransformersRuntimeAdapter

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    adapter = TransformersRuntimeAdapter(
        model_dir, quantization=None, max_context_tokens=4096
    )
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()
    first = adapter.create(brain_id=first_brain, runtime_id="rt_first")
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    assert len(created) == 2
    assert created[0] is not created[1]
    assert first.isolation == "dedicated_weights"
    assert second.isolation == "dedicated_weights"
    assert first.context is not second.context


def test_transformers_adapter_reports_a_missing_artifact(tmp_path):
    from app.brain_manager.runtime.base import RuntimeProvisioningError
    from app.brain_manager.runtime.transformers import TransformersRuntimeAdapter

    adapter = TransformersRuntimeAdapter(
        tmp_path / "absent", quantization=None, max_context_tokens=4096
    )
    with pytest.raises(RuntimeProvisioningError) as exc:
        adapter.create(brain_id=uuid.uuid4(), runtime_id="rt_x")
    assert exc.value.code == "model_artifact_missing"


def test_external_adapter_builds_one_handle_per_brain():
    """The external adapter gives every Brain its own provider handle."""
    from app.brain_manager.runtime.external import ExternalProviderRuntimeAdapter

    adapter = ExternalProviderRuntimeAdapter(
        model_id="test-model", max_context_tokens=4096
    )
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()
    first = adapter.create(brain_id=first_brain, runtime_id="rt_first")
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    assert first is not second
    assert first.context is not second.context
    assert first.isolation == "context_isolated_shared_weights"
    assert second.isolation == "context_isolated_shared_weights"


@pytest.mark.asyncio_compatible
def test_lifecycle_stop_and_restart_replace_the_runtime(monkeypatch):
    """Stopping a Brain's runtime releases it; restarting creates a new one."""
    import asyncio

    monkeypatch.setenv("BRAIN_RUNTIME_MODE", "simulated")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")

    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle.manager import BrainRuntimeManager

    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()

    async def _run():
        manager = BrainRuntimeManager()
        brain_id = uuid.uuid4()

        class _Brain:
            def __init__(self):
                self.brain_id = brain_id
                self.llm_runtime_status = "unregistered"
                self.runtime_last_error = None
                self.provisioning_error = None
                self.runtime_started_at = None
                self.updated_at = None

        class _Session:
            async def commit(self):
                return None

            async def scalar(self, *args, **kwargs):
                return 0

        brain = _Brain()
        session = _Session()

        first = await manager.provision_runtime(session, brain)
        assert first.succeeded
        original = manager.handle(brain_id)
        assert original is not None

        # A second provisioning request reuses the *same* runtime.
        again = await manager.provision_runtime(session, brain)
        assert again.runtime_id == first.runtime_id
        assert manager.handle(brain_id) is original

        # Stopping releases the runtime.
        released = await manager.stop_runtime(session, brain)
        assert released is True
        assert manager.handle(brain_id) is None
        assert original.health().status == "stopped"

        # Restarting creates a *new* runtime with a new identifier.
        restarted = await manager.restart_runtime(session, brain)
        assert restarted.succeeded
        assert restarted.runtime_id != first.runtime_id
        assert manager.handle(brain_id) is not original

        await manager.shutdown()
        assert manager.handle(brain_id) is None

    asyncio.run(_run())
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()


def test_lifecycle_manager_is_a_process_wide_singleton():
    from app.brain_manager.lifecycle.manager import (
        get_runtime_manager,
        reset_runtime_manager,
    )

    reset_runtime_manager()
    first = get_runtime_manager()
    second = get_runtime_manager()
    assert first is second
    reset_runtime_manager()
    assert get_runtime_manager() is not first
