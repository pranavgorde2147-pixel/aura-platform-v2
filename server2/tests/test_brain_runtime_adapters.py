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
from pathlib import Path

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
    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "brain-resources"))

    from app.brain_manager.runtime.llama_cpp import LlamaCppRuntimeAdapter

    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"not-a-real-gguf")

    adapter = LlamaCppRuntimeAdapter(
        artifact, context_tokens=2048, max_context_tokens=4096
    )
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()
    first_resource_root = f"Avery_Example_{first_brain}"
    private_model = (
        tmp_path
        / "brain-resources"
        / first_resource_root
        / "personal_llm"
        / "model_weights"
        / artifact.name
    )
    private_model.parent.mkdir(parents=True)
    private_model.write_bytes(b"private model copy")
    first = adapter.create(
        brain_id=first_brain,
        runtime_id="rt_first",
        resource_root=first_resource_root,
    )
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    # Existing per-Brain weights are preferred, while other Brains use the
    # configured artifact without copying or modifying it.
    assert len(created) == 2
    assert created[0] is not created[1]
    assert first.isolation == "dedicated_runtime_per_brain_weights"
    assert second.isolation == "dedicated_runtime_shared_weights"
    assert first.context is not second.context
    assert first.context.brain_id == first_brain
    assert second.context.brain_id == second_brain
    first_path = Path(created[0].kwargs["model_path"])
    second_path = Path(created[1].kwargs["model_path"])
    assert first_path == private_model.resolve()
    assert first_path.read_bytes() == b"private model copy"
    assert second_path == artifact.resolve()
    assert second_path.read_bytes() == b"not-a-real-gguf"


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


def test_private_model_provisioning_copies_once_per_brain(tmp_path, monkeypatch):
    import asyncio

    from app.brain_manager.runtime.artifacts import provision_private_model_artifact

    root = tmp_path / "brain-resources"
    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(root))
    source = tmp_path / "Qwen3.5-0.8B-Q4_K_M.gguf"
    source.write_bytes(b"complete model artifact")
    brains = (uuid.uuid4(), uuid.uuid4())

    async def _provision():
        paths = []
        for brain_id in brains:
            paths.append(
                await provision_private_model_artifact(
                    source,
                    brain_id=brain_id,
                    resource_root=f"User_{brain_id}",
                )
            )
        original_mtime = paths[0].stat().st_mtime_ns
        retried = await provision_private_model_artifact(
            source,
            brain_id=brains[0],
            resource_root=f"User_{brains[0]}",
        )
        assert retried == paths[0]
        assert paths[0].stat().st_mtime_ns == original_mtime
        return paths

    first, second = asyncio.run(_provision())

    assert first.read_bytes() == second.read_bytes() == source.read_bytes()
    assert first != second
    assert first.stat().st_ino != second.stat().st_ino
    assert first.parent.name == second.parent.name == "model_weights"


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
    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "brain-resources"))

    from app.brain_manager.runtime.transformers import TransformersRuntimeAdapter

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    adapter = TransformersRuntimeAdapter(
        model_dir, quantization=None, max_context_tokens=4096
    )
    first_brain, second_brain = uuid.uuid4(), uuid.uuid4()
    first_resource_root = f"Private_Weights_{first_brain}"
    private_model = (
        tmp_path
        / "brain-resources"
        / first_resource_root
        / "personal_llm"
        / "model_weights"
    )
    private_model.mkdir(parents=True)
    (private_model / "config.json").write_text("{}", encoding="utf-8")
    first = adapter.create(
        brain_id=first_brain,
        runtime_id="rt_first",
        resource_root=first_resource_root,
    )
    second = adapter.create(brain_id=second_brain, runtime_id="rt_second")

    assert len(created) == 2
    assert created[0] is not created[1]
    assert first.isolation == "dedicated_runtime_per_brain_weights"
    assert second.isolation == "dedicated_runtime_shared_weights"
    assert first.context is not second.context
    assert first.model_path == private_model.resolve()
    assert second.model_path == model_dir.resolve()
    assert first.model_path.is_dir()
    assert second.model_path.is_dir()


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


def test_lifecycle_reclaims_only_idle_runtime_slots(monkeypatch):
    """A full pool reuses private weights without stopping an in-flight Brain."""
    import asyncio

    monkeypatch.setenv("BRAIN_RUNTIME_MODE", "simulated")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.setenv("BRAIN_MAX_CONCURRENT_RUNTIMES", "1")

    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle.manager import BrainRuntimeManager
    from app.brain_manager.registry.enums import ResourceAvailability
    from app.brain_manager.registry.service import BrainRegistryService
    from app.brain_manager.runtime.simulated import SimulatedRuntimeAdapter

    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    registry = {}

    async def get_brain(session, brain_id):
        return registry.get(brain_id)

    monkeypatch.setattr(BrainRegistryService, "get", staticmethod(get_brain))

    class Capacity:
        max_runtimes = 1
        max_concurrent_requests_per_brain = 1

        def __init__(self):
            self.reserved = set()

        async def reserve(self, session, brain_id):
            if brain_id in self.reserved:
                return ResourceAvailability.AVAILABLE, "already reserved"
            if self.reserved:
                return ResourceAvailability.WAITING, "runtime pool full"
            self.reserved.add(brain_id)
            return ResourceAvailability.AVAILABLE, "reserved"

        async def release(self, brain_id):
            self.reserved.discard(brain_id)

    class Brain:
        def __init__(self, brain_id):
            self.brain_id = brain_id
            self.resource_root = f"brain_{brain_id}"
            self.llm_runtime_status = "unregistered"
            self.llm_runtime_id = None
            self.runtime_isolation = None
            self.runtime_last_error = None
            self.provisioning_error = None
            self.runtime_started_at = None
            self.updated_at = None

    class Session:
        async def commit(self):
            return None

    async def _run():
        manager = BrainRuntimeManager(
            adapter=SimulatedRuntimeAdapter(),
            capacity=Capacity(),
        )
        session = Session()
        first_brain = Brain(uuid.uuid4())
        second_brain = Brain(uuid.uuid4())
        registry[first_brain.brain_id] = first_brain
        registry[second_brain.brain_id] = second_brain

        first = await manager.provision_runtime(session, first_brain)
        assert first.succeeded
        first_runtime = manager.handle(first_brain.brain_id)
        assert first_runtime is not None

        await manager.request_started(first_brain.brain_id)
        blocked = await manager.provision_runtime(session, second_brain)
        assert not blocked.succeeded
        assert manager.handle(first_brain.brain_id) is first_runtime
        assert first_runtime.health().healthy

        await manager.request_finished(first_brain.brain_id)
        second = await manager.provision_runtime(session, second_brain)
        assert second.succeeded
        assert manager.handle(first_brain.brain_id) is None
        assert first_runtime.health().status == "stopped"
        assert second_brain.llm_runtime_status == "ready_simulated"

        restored = await manager.provision_runtime(session, first_brain)
        assert restored.succeeded
        assert manager.handle(second_brain.brain_id) is None
        assert manager.handle(first_brain.brain_id) is not None
        await manager.shutdown()

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
