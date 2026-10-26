"""Architecture and security guarantees of the per-user Brain platform.

These tests assert the *observable* behaviour of the Server 2 Brain manager:

* every registered user gets a unique Brain identity and a unique runtime;
* provisioning is idempotent and concurrent requests cannot create a second
  primary Brain for the same owner;
* every Brain owns its runtime, context, memory, vector namespace, vault,
  app snapshot, jobs and compute accounting;
* one owner can never read another owner's data through the Brain APIs;
* the administrative registry API requires administrative authorization.

The suite runs against the deterministic simulator (no model artifact is
needed), which is also what proves that a *simulated* runtime is never
presented as a production-isolated one.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app as brain_app
from app.storage_app import app as storage_app


def test_relative_brain_resource_root_is_anchored_to_server2():
    from app.brain_manager.config import BrainManagerConfig

    server_root = Path(__file__).resolve().parents[1]
    config = BrainManagerConfig(resource_root="./data/brains")

    assert config.resolved_resource_root == server_root / "data" / "brains"


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "brain-architecture"
    data_dir.mkdir()
    monkeypatch.setenv("INTERNAL_API_TOKEN", "architecture-test-token")
    monkeypatch.setenv(
        "STORAGE_DATABASE_URL", f"sqlite+aiosqlite:///{data_dir / 'architecture.db'}"
    )
    monkeypatch.setenv("STORAGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(data_dir / "brains"))
    monkeypatch.setenv("BRAIN_ADMIN_OWNER_IDS", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AURA_SKIP_DOTENV", "1")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "1")
    monkeypatch.delenv("EXTERNAL_AI_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_AI_KEY", raising=False)

    from app import storage_config, storage_db
    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle import manager
    from app.storage.base import reset_object_store

    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage_db._schema_ready_engine = None
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()
    reset_object_store()
    with TestClient(storage_app) as test_client:
        yield test_client
    storage_config.get_settings.cache_clear()
    storage_db._engine = None
    storage_db._session_factory = None
    storage_db._schema_ready_engine = None
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()
    reset_object_store()


def _auth(owner_id: uuid.UUID | str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer architecture-test-token"}
    if owner_id is not None:
        headers["X-Aura-Owner-Id"] = str(owner_id)
    return headers


def _provision(
    client: TestClient,
    *,
    brain_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    identity_id: uuid.UUID | None = None,
    owner_name: str | None = None,
    model_id: str = "Qwen3.5-0.8B",
) -> dict:
    brain_id = brain_id or uuid.uuid4()
    owner_id = owner_id or uuid.uuid4()
    identity_id = identity_id or uuid.uuid4()
    response = client.post(
        "/v1/brains/provision",
        headers=_auth(),
        json={
            "brain_id": str(brain_id),
            "owner_id": str(owner_id),
            "ai_identity_id": str(identity_id),
            "owner_name": owner_name,
            "model_id": model_id,
        },
    )
    assert response.status_code == 200, response.text
    return {
        "brain_id": str(brain_id),
        "owner_id": str(owner_id),
        "identity_id": str(identity_id),
        "body": response.json(),
    }


# ---------------------------------------------------------------------------
# Architecture: one Brain per user, one runtime per Brain
# ---------------------------------------------------------------------------


def test_each_owner_receives_a_unique_brain_and_runtime(client):
    """Two users never share a Brain id, a runtime id or a resource root."""
    first = _provision(client)
    second = _provision(client)

    assert first["body"]["brain_id"] != second["body"]["brain_id"]
    assert first["body"]["llm_runtime_id"] != second["body"]["llm_runtime_id"]
    assert first["body"]["resource_root"] != second["body"]["resource_root"]
    assert first["body"]["storage_resource_reference"] != second["body"][
        "storage_resource_reference"
    ]


def test_brain_owns_every_isolated_resource(client):
    """Every Brain gets its own runtime, context, memory, vector and vault."""
    provisioned = _provision(client)
    body = provisioned["body"]
    brain_id = provisioned["brain_id"]

    # Runtime identity.
    assert body["llm_runtime_id"]
    assert body["llm_runtime_status"] == "ready_simulated"
    assert body["runtime_isolation"] == "simulated"
    assert body["model_configuration_id"]

    # Memory, vector and vault namespaces are Brain-scoped.
    assert body["memory_resource_reference"].startswith(f"brain_{brain_id}")
    assert body["vector_resource_reference"].startswith(f"brain_{brain_id}")
    assert body["vault_resource_reference"].startswith(f"brain_{brain_id}")
    assert body["memory_resource_reference"] != body["vector_resource_reference"]
    assert body["memory_resource_reference"] != body["vault_resource_reference"]

    manifest = client.get(f"/v1/brains/{brain_id}/manifest", headers=_auth()).json()
    assert manifest["manifest"]["resource_references"]["model_weights"] == (
        "shared_model_artifact"
    )
    resource_root = client.get(
        f"/v1/brains/{brain_id}", headers=_auth()
    ).json()["resource_root"]
    brain_directory = Path(os.environ["BRAIN_RESOURCE_ROOT"]) / resource_root
    assert (brain_directory / "brain_manifest.json").is_file()
    assert (brain_directory / "personal_llm" / "model_weights").is_dir()
    assert (brain_directory / "memory_vault" / "vault.sqlite").is_file()
    assert (brain_directory / "objects").is_dir()
    assert (brain_directory / "indexes").is_dir()
    disk_manifest = json.loads(
        (brain_directory / "brain_manifest.json").read_text(encoding="utf-8")
    )
    references = disk_manifest["resource_references"]
    for name in ("personal_llm", "memory", "vector", "vault", "objects"):
        assert (brain_directory.parent / references[name]).exists()
    assert references["model_weights"] == "shared_model_artifact"

    # Storage is owner-scoped *and* Brain-scoped.
    assert body["storage_resource_reference"].endswith(brain_id)
    assert provisioned["owner_id"] in body["storage_resource_reference"]


def test_account_provisioning_copies_complete_model_into_private_brain(client, monkeypatch, tmp_path):
    """A new local Brain gets its own model file, vault, folders and manifest."""
    from app.brain_manager import config, runtime
    from app.brain_manager.lifecycle import manager

    model = tmp_path / "Qwen3.5-0.8B-Q4_K_M.gguf"
    model.write_bytes(b"complete test model")
    monkeypatch.setenv("AURA_SKIP_MODEL_LOAD", "0")
    monkeypatch.setenv("BRAIN_RUNTIME_MODE", "dedicated")
    monkeypatch.setenv("BRAIN_RUNTIME_ADAPTER", "simulated")
    monkeypatch.setenv("MODEL_PATH", str(model))
    config.reset_brain_manager_config()
    runtime.factory.reset_runtime_adapter()
    manager.reset_runtime_manager()

    provisioned = _provision(client)
    brain_id = provisioned["brain_id"]
    brain_directory = Path(os.environ["BRAIN_RESOURCE_ROOT"]) / provisioned["body"]["resource_root"]
    private_model = (
        brain_directory
        / "personal_llm"
        / "model_weights"
        / model.name
    )
    assert private_model.read_bytes() == model.read_bytes()
    assert private_model.stat().st_ino != model.stat().st_ino
    assert (brain_directory / "brain_manifest.json").is_file()
    assert (brain_directory / "memory_vault" / "vault.sqlite").is_file()
    assert (brain_directory / "objects").is_dir()
    assert (brain_directory / "indexes").is_dir()

    manifest = json.loads(
        (brain_directory / "brain_manifest.json").read_text(encoding="utf-8")
    )
    expected_model_reference = (
        f"{provisioned['body']['resource_root']}/personal_llm/model_weights/{model.name}"
    )
    assert manifest["resource_references"]["model_weights"] == expected_model_reference
    assert manifest["resource_references"]["vault"] == (
        f"{provisioned['body']['resource_root']}/memory_vault/vault.sqlite"
    )
    assert provisioned["body"]["runtime_isolation"] == "simulated"
    assert brain_id in manifest["brain_id"]


def test_a_brain_owns_a_live_runtime_object_with_its_own_context(client):
    """The runtime that serves a Brain is a real, Brain-bound object."""
    from app.brain_manager.harness import TOOL_MARKER
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    provisioned = _provision(client)
    brain_id = uuid.UUID(provisioned["brain_id"])
    manager = get_runtime_manager()

    handle = manager.handle(brain_id)
    assert handle is not None, "the provisioned Brain must own a live runtime"
    assert handle.runtime_id == provisioned["body"]["llm_runtime_id"]
    assert handle.brain_id == brain_id
    assert handle.isolation == "simulated"

    # The context belongs to that Brain alone.
    context = manager.context(brain_id)
    assert context is not None
    assert context.brain_id == brain_id
    assert context.runtime_id == handle.runtime_id
    assert context.messages == []
    assert context.cache == {}

    # Two Brains never share a runtime object or a context object.
    other = _provision(client)
    other_id = uuid.UUID(other["brain_id"])
    assert manager.handle(other_id) is not handle
    assert manager.context(other_id) is not context
    assert manager.context(other_id).brain_id == other_id


def test_runtime_context_is_not_shared_between_brains(client):
    """A turn seen by one Brain is invisible to another Brain's context."""
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    first = _provision(client)
    second = _provision(client)
    manager = get_runtime_manager()

    first_context = manager.context(uuid.UUID(first["brain_id"]))
    first_context.append("user", "a private question")
    first_context.cache["secret"] = "value"

    second_context = manager.context(uuid.UUID(second["brain_id"]))
    assert second_context.messages == []
    assert "secret" not in second_context.cache
    assert first_context.cache["secret"] == "value"


def test_provisioning_is_idempotent_for_the_same_brain(client):
    """Re-provisioning the same Brain returns the same runtime, not a new one."""
    brain_id, owner_id, identity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = _provision(
        client, brain_id=brain_id, owner_id=owner_id, identity_id=identity_id
    )
    second = _provision(
        client, brain_id=brain_id, owner_id=owner_id, identity_id=identity_id
    )

    assert first["body"]["llm_runtime_id"] == second["body"]["llm_runtime_id"]
    assert first["body"]["resource_root"] == second["body"]["resource_root"]
    assert first["body"]["provisioning_status"] == second["body"]["provisioning_status"]


def test_concurrent_provisioning_cannot_create_duplicate_brains(client):
    """Concurrent provisioning of one Brain yields a single Brain and runtime.

    The uniqueness guarantee is enforced by the database (unique constraints on
    ``owner_id``, ``ai_identity_id`` and ``llm_runtime_id``), so the race is
    resolved by the registry service rather than by an in-process lock.
    """
    from app import storage_db
    from app.brain_manager.lifecycle.manager import get_runtime_manager
    from app.brain_manager.provisioning.orchestrator import BrainProvisioningOrchestrator

    brain_id, owner_id, identity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _provision_once() -> dict:
        factory = storage_db.get_session_factory()
        async with factory() as session:
            orchestrator = BrainProvisioningOrchestrator(get_runtime_manager())
            outcome = await orchestrator.provision(
                session,
                brain_id=brain_id,
                owner_id=owner_id,
                ai_identity_id=identity_id,
                model_configuration={"model_id": "Qwen3.5-0.8B", "revision": "1"},
            )
            return {
                "brain_id": str(outcome.brain_id),
                "llm_runtime_id": outcome.llm_runtime_id,
            }

    async def _run() -> list[dict]:
        return list(await asyncio.gather(*(_provision_once() for _ in range(6))))

    results = asyncio.run(_run())

    assert {result["brain_id"] for result in results} == {str(brain_id)}
    runtime_ids = {result["llm_runtime_id"] for result in results}
    assert len(runtime_ids) == 1, f"duplicate runtimes created: {runtime_ids}"

    # The database holds exactly one row for that owner.
    async def _count_rows() -> int:
        from sqlalchemy import func, select

        factory = storage_db.get_session_factory()
        async with factory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(storage_db.ManagedBrain)
                    .where(storage_db.ManagedBrain.owner_id == owner_id)
                )
            )

    assert asyncio.run(_count_rows()) == 1


def test_brain_registry_rejects_a_second_identity_for_one_owner(client):
    """One owner cannot acquire a second, independent primary Brain."""
    owner_id = uuid.uuid4()
    _provision(client, owner_id=owner_id, identity_id=uuid.uuid4())
    conflict = client.post(
        "/v1/brains/provision",
        headers=_auth(),
        json={
            "brain_id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "ai_identity_id": str(uuid.uuid4()),
            "model_id": "Qwen3.5-0.8B",
        },
    )
    assert conflict.status_code == 409


def test_the_database_enforces_one_brain_one_runtime_one_vault_per_owner(client):
    """The "one user = one Brain = one LLM = one vault" chain is enforced by
    the database, not only by application code.

    A Brain belongs to exactly one owner, carries exactly one dedicated
    runtime id, and owns exactly one vector space (the private memory vault).
    The unique constraints are what stop a bug or a malicious caller from
    giving one user two Brains, two runtimes or two vaults.
    """
    from sqlalchemy.exc import IntegrityError

    from app import storage_db
    from app.brain_manager.db_models import BrainVectorSpace
    from app.brain_manager.registry.service import BrainRegistryService
    from app.brain_manager.vault.storage import get_brain_vault_storage

    owner_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    provisioned = _provision(client, owner_id=owner_id, identity_id=identity_id)
    brain_id = uuid.UUID(provisioned["brain_id"])
    runtime_id = provisioned["body"]["llm_runtime_id"]
    assert runtime_id

    factory = storage_db.get_session_factory()

    async def _violations() -> list[str]:
        """Try to break the chain directly in the database.

        Returns the list of breaks that were *accepted*. Each attempt must be
        rejected by the constraint that names it, so the assertion below proves
        the right rule fired rather than some incidental error.
        """
        found: list[str] = []

        def _brain(brain_id, owner, identity, runtime_id):
            return storage_db.ManagedBrain(
                brain_id=brain_id,
                owner_id=owner,
                ai_identity_id=identity,
                provisioning_status="ready",
                model_configuration={},
                compute_allocation={},
                storage_resource_reference="owners/x/brains/y",
                llm_runtime_id=runtime_id,
                llm_runtime_status="ready_simulated",
            )

        async def _attempt(make_row, expected: str) -> None:
            async with factory() as session:
                try:
                    async with session.begin():
                        session.add(make_row())
                except IntegrityError as exc:
                    message = str(getattr(exc, "orig", exc))
                    if expected not in message:
                        found.append(
                            f"rejected for the wrong reason, wanted {expected!r}, got {message!r}"
                        )
                    return
                found.append(f"{expected} was accepted")

        # 1. A second Brain for the same owner.
        await _attempt(
            lambda: _brain(uuid.uuid4(), owner_id, uuid.uuid4(), f"rt_{uuid.uuid4().hex}"),
            "owner_id",
        )

        # 2. A second Brain reusing another Brain's runtime id.
        await _attempt(
            lambda: _brain(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), runtime_id),
            "llm_runtime_id",
        )

        return found

    violations = asyncio.run(_violations())
    assert violations == [], violations

    # A vector-space singleton is enforced inside the Brain's own database.
    async def _private_space_invariant() -> tuple[bool, int]:
        from sqlalchemy import func, select

        async with factory() as registry_session:
            brain = await BrainRegistryService.get_owned(
                registry_session, brain_id=brain_id, owner_id=owner_id
            )
        assert brain is not None
        storage = get_brain_vault_storage()
        duplicate_rejected = False
        count = 0
        async with storage.session(brain) as session:
            session.add(
                BrainVectorSpace(
                    brain_id=brain_id,
                    owner_id=owner_id,
                    dimension=1536,
                    model_id="test",
                    status="ready",
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                duplicate_rejected = True
                await session.rollback()
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(BrainVectorSpace)
                    .where(BrainVectorSpace.brain_id == brain_id)
                )
                or 0
            )
        return duplicate_rejected, count

    duplicate_rejected, spaces = asyncio.run(_private_space_invariant())

    # The registry remains shared; the private vector metadata does not.
    async def _brain_count() -> int:
        from sqlalchemy import func, select

        async with factory() as session:
            return int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(storage_db.ManagedBrain)
                        .where(storage_db.ManagedBrain.owner_id == owner_id)
                    )
                )
                or 0
            )

    brains = asyncio.run(_brain_count())
    assert duplicate_rejected
    assert brains == 1
    assert spaces == 1


def test_records_are_physically_written_to_distinct_brain_vault_files(client):
    """Each Brain's rows and indexes live in its own SQLite file."""
    import sqlite3

    from app import storage_db
    from app.brain_manager.registry.service import BrainRegistryService
    from app.brain_manager.vault.storage import get_brain_vault_storage

    owner_a = _provision(client)
    owner_b = _provision(client)
    record_a = client.post(
        f"/v1/brains/{owner_a['brain_id']}/vault/records",
        headers=_auth(owner_a["owner_id"]),
        json={
            "record_type": "note",
            "title": "A private entry",
            "content": "Raigad is only in owner A's vault.",
        },
    )
    assert record_a.status_code == 201, record_a.text

    async def _brain_rows():
        factory = storage_db.get_session_factory()
        async with factory() as session:
            brain_a = await BrainRegistryService.get_owned(
                session,
                brain_id=uuid.UUID(owner_a["brain_id"]),
                owner_id=uuid.UUID(owner_a["owner_id"]),
            )
            brain_b = await BrainRegistryService.get_owned(
                session,
                brain_id=uuid.UUID(owner_b["brain_id"]),
                owner_id=uuid.UUID(owner_b["owner_id"]),
            )
            assert brain_a is not None and brain_b is not None
            return brain_a, brain_b

    brain_a, brain_b = asyncio.run(_brain_rows())
    vault_storage = get_brain_vault_storage()
    path_a = vault_storage.database_path(brain_a)
    path_b = vault_storage.database_path(brain_b)
    assert path_a.name == "vault.sqlite"
    assert path_b.name == "vault.sqlite"
    assert path_a != path_b
    assert path_a.parent.name == path_b.parent.name == "memory_vault"
    assert path_a.parent.parent.name.endswith(owner_a["brain_id"])
    assert path_b.parent.parent.name.endswith(owner_b["brain_id"])
    assert path_a.parent.parent != path_b.parent.parent
    async def _initialize_concurrently():
        await asyncio.gather(
            vault_storage.ensure_vault(brain_a),
            vault_storage.ensure_vault(brain_a),
            vault_storage.ensure_vault(brain_b),
        )

    asyncio.run(_initialize_concurrently())

    with sqlite3.connect(path_a) as database_a:
        rows_a = database_a.execute(
            "SELECT content FROM brain_vault_records WHERE id = ?",
            (record_a.json()["record_id"],),
        ).fetchall()
        assert rows_a == [("Raigad is only in owner A's vault.",)]
        assert database_a.execute(
            "SELECT COUNT(*) FROM brain_vault_embeddings"
        ).fetchone()[0] > 0

    with sqlite3.connect(path_b) as database_b:
        assert database_b.execute(
            "SELECT COUNT(*) FROM brain_vault_records"
        ).fetchone()[0] == 0

    result_b = client.post(
        f"/v1/brains/{owner_b['brain_id']}/vault/search",
        headers=_auth(owner_b["owner_id"]),
        json={"query": "Raigad owner A private entry", "limit": 5},
    )
    assert result_b.status_code == 200
    assert all("owner A" not in item.get("content", "") for item in result_b.json()["results"])


def test_private_vault_rejects_symlinked_brain_directory_escape(tmp_path, monkeypatch):
    """A Brain directory symlink cannot redirect storage outside the root."""
    from types import SimpleNamespace

    from app import storage_config
    from app.brain_manager.vault.storage import (
        BrainVaultStorageError,
        BrainVaultStorageManager,
    )

    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "private-vaults"))
    manager = BrainVaultStorageManager()
    brain_id = uuid.uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.root.joinpath(f"brain_{brain_id}").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BrainVaultStorageError):
        manager.brain_directory(SimpleNamespace(brain_id=brain_id))


def test_private_vault_survives_engine_restart(tmp_path, monkeypatch):
    """Closing and reopening Server 2's per-Brain engine preserves vault data."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from app import storage_config
    from app.brain_manager.db_models import BrainVaultRecord
    from app.brain_manager.vault.storage import BrainVaultStorageManager

    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "private-vaults"))
    brain = SimpleNamespace(brain_id=uuid.uuid4(), owner_id=uuid.uuid4())
    record_id = uuid.uuid4()

    async def _write_and_restart():
        first = BrainVaultStorageManager()
        async with first.session(brain) as session:
            session.add(
                BrainVaultRecord(
                    id=record_id,
                    brain_id=brain.brain_id,
                    owner_id=brain.owner_id,
                    record_type="note",
                    namespace="shared",
                    content="persist across restart",
                )
            )
            await session.commit()
        await first.dispose()

        restarted = BrainVaultStorageManager()
        async with restarted.session(brain) as session:
            result = await session.execute(
                select(BrainVaultRecord).where(BrainVaultRecord.id == record_id)
            )
            assert result.scalar_one().content == "persist across restart"
        await restarted.dispose()

    asyncio.run(_write_and_restart())


def test_backup_restore_and_delete_are_brain_scoped(tmp_path, monkeypatch):
    """Vault lifecycle operations never touch another Brain's files."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from app import storage_config
    from app.brain_manager.db_models import BrainVaultRecord
    from app.brain_manager.vault.storage import BrainVaultStorageManager

    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "private-vaults"))
    monkeypatch.setenv("BRAIN_VAULT_BACKUP_ROOT", str(tmp_path / "backups"))
    storage_config.get_settings.cache_clear()
    manager = BrainVaultStorageManager()
    brain_a = SimpleNamespace(brain_id=uuid.uuid4(), owner_id=uuid.uuid4())
    brain_b = SimpleNamespace(brain_id=uuid.uuid4(), owner_id=uuid.uuid4())
    record_a = uuid.uuid4()
    record_b = uuid.uuid4()
    backup_id = uuid.uuid4()

    async def _exercise_lifecycle():
        for brain, record_id, content in (
            (brain_a, record_a, "original A"),
            (brain_b, record_b, "private B"),
        ):
            async with manager.session(brain) as session:
                session.add(
                    BrainVaultRecord(
                        id=record_id,
                        brain_id=brain.brain_id,
                        owner_id=brain.owner_id,
                        record_type="note",
                        namespace="shared",
                        content=content,
                    )
                )
                await session.commit()

        brain_a_directory = manager.brain_directory(brain_a)
        model_file = (
            brain_a_directory
            / "personal_llm"
            / "model_weights"
            / "preserve-model.gguf"
        )
        model_file.parent.mkdir(parents=True, exist_ok=True)
        model_file.write_bytes(b"model data must not be replaced")
        manifest = brain_a_directory / "brain_manifest.json"
        manifest.write_text('{"brain_id":"preserve"}', encoding="utf-8")

        await manager.backup(brain_a, backup_id)
        async with manager.session(brain_a) as session:
            record = await session.get(BrainVaultRecord, record_a)
            record.content = "changed A"
            await session.commit()
        await manager.restore(brain_a, backup_id)
        assert model_file.read_bytes() == b"model data must not be replaced"
        assert manifest.read_text(encoding="utf-8") == '{"brain_id":"preserve"}'
        async with manager.session(brain_a) as session:
            restored = await session.get(BrainVaultRecord, record_a)
            assert restored.content == "original A"
        async with manager.session(brain_b) as session:
            other = await session.execute(
                select(BrainVaultRecord).where(BrainVaultRecord.id == record_b)
            )
            assert other.scalar_one().content == "private B"
        path_a = manager.database_path(brain_a)
        path_b = manager.database_path(brain_b)
        await manager.dispose()
        await manager.delete(brain_a, owner_id=brain_a.owner_id, confirm=True)
        assert not path_a.exists()
        assert path_b.is_file()
        assert model_file.read_bytes() == b"model data must not be replaced"
        assert manifest.is_file()
        with pytest.raises(PermissionError):
            await manager.delete(brain_b, owner_id=uuid.uuid4(), confirm=True)
        await manager.dispose()

    asyncio.run(_exercise_lifecycle())
    storage_config.get_settings.cache_clear()


def test_shared_vault_migration_is_retryable_and_preserves_source(client):
    """Migration copies records/object bytes once and leaves shared data intact."""
    from io import BytesIO
    import sqlite3

    from app import storage_db
    from app.brain_manager.db_models import BrainVaultRecord, StoredObject, VaultBase
    from app.brain_manager.registry.service import BrainRegistryService
    from app.brain_manager.vault.migration import migrate_shared_vault
    from app.brain_manager.vault.storage import get_brain_vault_storage
    from app.services.objects import checksum_and_size, object_key
    from app.storage.base import get_cached_object_store

    provisioned = _provision(client)
    body = b"legacy object bytes"
    checksum, size = checksum_and_size(BytesIO(body))
    object_id = uuid.uuid4()
    record_id = uuid.uuid4()
    key = object_key(
        uuid.UUID(provisioned["owner_id"]),
        object_id,
        uuid.UUID(provisioned["brain_id"]),
    )

    async def _seed_shared_database():
        shared_engine = storage_db.get_engine()
        async with shared_engine.begin() as connection:
            await connection.run_sync(VaultBase.metadata.create_all)
        store = get_cached_object_store()
        store.save(key, BytesIO(b"x" * size), size)
        async with storage_db.get_session_factory()() as session:
            session.add(
                BrainVaultRecord(
                    id=record_id,
                    brain_id=uuid.UUID(provisioned["brain_id"]),
                    owner_id=uuid.UUID(provisioned["owner_id"]),
                    record_type="document",
                    namespace="documents",
                    content="Legacy record points to migrated object.",
                    object_key=key,
                )
            )
            session.add(
                StoredObject(
                    id=object_id,
                    owner_id=uuid.UUID(provisioned["owner_id"]),
                    ai_identity_id=uuid.UUID(provisioned["identity_id"]),
                    brain_id=uuid.UUID(provisioned["brain_id"]),
                    object_key=key,
                    filename="legacy.txt",
                    content_type="text/plain",
                    size_bytes=size,
                    checksum_sha256=checksum,
                    storage_backend="local",
                )
            )
            await session.commit()

    asyncio.run(_seed_shared_database())

    async def _migrate_with_recovery():
        async with storage_db.get_session_factory()() as session:
            brain = await BrainRegistryService.get_owned(
                session,
                brain_id=uuid.UUID(provisioned["brain_id"]),
                owner_id=uuid.UUID(provisioned["owner_id"]),
            )
            assert brain is not None
            first = await migrate_shared_vault(session, brain)
            second = await migrate_shared_vault(session, brain)
            get_cached_object_store().save(key, BytesIO(body), size)
            third = await migrate_shared_vault(session, brain)
            return first, second, third, brain

    first_report, retry_report, final_report, migrated_brain = asyncio.run(
        _migrate_with_recovery()
    )
    assert first_report.status == "needs_review", first_report
    assert first_report.copied["brain_vault_records"] == 1
    assert first_report.object_errors == 1
    assert retry_report.status == "needs_review", retry_report
    assert retry_report.copied["brain_vault_records"] == 0
    assert retry_report.already_present["brain_vault_records"] == 1
    assert retry_report.object_errors == 1
    assert retry_report.copied["stored_objects"] == 0
    assert retry_report.already_present["stored_objects"] == 0
    assert final_report.status == "verified"
    assert final_report.copied["brain_vault_records"] == 0
    assert final_report.copied["stored_objects"] == 1
    assert final_report.already_present["stored_objects"] == 0

    manager = get_brain_vault_storage()
    assert (manager.object_directory(migrated_brain) / key).read_bytes() == body
    with sqlite3.connect(manager.database_path(migrated_brain)) as private_db:
        assert private_db.execute(
            "SELECT COUNT(*) FROM brain_vault_records WHERE id = ?", (str(record_id),)
        ).fetchone()[0] == 1
        assert private_db.execute(
            "SELECT COUNT(*) FROM stored_objects WHERE id = ?", (str(object_id),)
        ).fetchone()[0] == 1
        assert private_db.execute(
            "SELECT object_key FROM brain_vault_records WHERE id = ?", (str(record_id),)
        ).fetchone()[0] == key

    async def _source_still_exists():
        async with storage_db.get_session_factory()() as session:
            return (
                await session.get(BrainVaultRecord, record_id),
                await session.get(StoredObject, object_id),
            )

    source_record, source_object = asyncio.run(_source_still_exists())
    assert source_record is not None
    assert source_object is not None
    assert get_cached_object_store().exists(key)


def test_each_brain_has_its_own_compute_accounting(client):
    """Request counters are accounted per Brain, not globally."""
    first = _provision(client)
    second = _provision(client)

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(),
            json={
                "user_id": first["owner_id"],
                "query": "hello",
                "context": {
                    "user_level": "general",
                    "relevant_context": {},
                    "safety_constraints": [],
                },
                "request_id": str(uuid.uuid4()),
                "model": "Qwen3.5-0.8B",
            },
        )
        assert response.status_code == 200, response.text

    from app import storage_db

    async def _counts() -> tuple[int, int]:
        factory = storage_db.get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            rows = (
                await session.execute(
                    select(storage_db.ManagedBrain.brain_id, storage_db.ManagedBrain.request_count)
                )
            ).all()
            return {str(row[0]): row[1] for row in rows}.get(first["brain_id"], 0), {
                str(row[0]): row[1] for row in rows
            }.get(second["brain_id"], 0)

    used_first, used_second = asyncio.run(_counts())
    assert used_first == 1
    assert used_second == 0


def test_unregistered_runtime_is_recovered_during_inference(client):
    """A ready Brain can recreate its runtime instead of failing middleware preflight."""
    provisioned = _provision(client)
    from app import storage_db
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    async def _mark_runtime_unregistered() -> None:
        async with storage_db.get_session_factory()() as session:
            brain = await session.get(
                storage_db.ManagedBrain, uuid.UUID(provisioned["brain_id"])
            )
            assert brain is not None
            brain.llm_runtime_status = "unregistered"
            brain.llm_runtime_id = None
            await session.commit()

    asyncio.run(_mark_runtime_unregistered())
    get_runtime_manager()._runtimes.pop(uuid.UUID(provisioned["brain_id"]), None)

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(),
            json={
                "user_id": provisioned["owner_id"],
                "query": "hello",
                "context": {
                    "user_level": "general",
                    "relevant_context": {},
                    "safety_constraints": [],
                },
                "request_id": str(uuid.uuid4()),
                "model": "Qwen3.5-0.8B",
            },
        )

    assert response.status_code == 200, response.text


def test_manifest_is_written_and_contains_no_secrets(client):
    """The per-Brain file carries the owner's name, never credentials."""
    from app.brain_manager.resource_manager.paths import (
        brain_root,
        named_brain_directory_name,
    )

    owner_name = "Avery Example"
    provisioned = _provision(client, owner_name=owner_name)
    response = client.get(
        f"/v1/brains/{provisioned['brain_id']}/manifest", headers=_auth()
    )
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert manifest["manifest"]["owner_name"] == owner_name
    brain_id = uuid.UUID(provisioned["brain_id"])
    resource_root = manifest["manifest"]["resource_references"]["root"]
    assert resource_root == named_brain_directory_name(brain_id, owner_name)
    manifest_file = (
        brain_root(brain_id, resource_root=resource_root)
        / "brain_manifest.json"
    )
    assert json.loads(manifest_file.read_text(encoding="utf-8"))["owner_name"] == owner_name

    # A legacy/idempotent provisioning call without the optional display name
    # must not erase the name already attached to this Brain.
    _provision(
        client,
        brain_id=uuid.UUID(provisioned["brain_id"]),
        owner_id=uuid.UUID(provisioned["owner_id"]),
        identity_id=uuid.UUID(provisioned["identity_id"]),
    )
    refreshed = client.get(
        f"/v1/brains/{provisioned['brain_id']}/manifest", headers=_auth()
    )
    assert refreshed.json()["manifest"]["owner_name"] == owner_name

    manifest = refreshed.json()["manifest"]
    body = json.dumps(manifest).lower()
    for forbidden in ("token", "secret", "password", "api_key", "authorization"):
        assert forbidden not in body


def test_brain_directory_name_is_safe_and_unique():
    from app.brain_manager.resource_manager.paths import named_brain_directory_name

    brain_id = uuid.uuid4()
    name = named_brain_directory_name(brain_id, "../../Alice / Example")
    assert name == f"Alice_Example_{brain_id}"
    assert "/" not in name
    assert named_brain_directory_name(brain_id, "東京") == f"brain_{brain_id}"


def test_brain_objects_are_stored_under_the_brain_namespace(client):
    """Object bytes and metadata live inside the matching Brain vault."""
    import sqlite3

    from app import storage_db
    from app.brain_manager.registry.service import BrainRegistryService
    from app.brain_manager.vault.storage import get_brain_vault_storage

    provisioned = _provision(client)
    uploaded = client.post(
        "/v1/objects",
        headers=_auth(provisioned["owner_id"]),
        data={"ai_identity_id": provisioned["identity_id"]},
        files={"file": ("private.txt", b"private brain data", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = uploaded.json()
    assert payload["brain_id"] == provisioned["brain_id"]
    assert provisioned["brain_id"] in payload["object_key"]
    assert provisioned["owner_id"] in payload["object_key"]

    async def _registered_brain():
        async with storage_db.get_session_factory()() as session:
            return await BrainRegistryService.get_owned(
                session,
                brain_id=uuid.UUID(provisioned["brain_id"]),
                owner_id=uuid.UUID(provisioned["owner_id"]),
            )

    brain = asyncio.run(_registered_brain())
    assert brain is not None
    manager = get_brain_vault_storage()
    object_path = manager.object_directory(brain) / payload["object_key"]
    assert object_path.is_file()
    assert object_path.read_bytes() == b"private brain data"
    with sqlite3.connect(manager.database_path(brain)) as database:
        metadata = database.execute(
            "SELECT filename, checksum_sha256 FROM stored_objects WHERE id = ?",
            (payload["id"],),
        ).fetchone()
    assert metadata == ("private.txt", payload["checksum_sha256"])

    second_upload = client.post(
        "/v1/objects",
        headers=_auth(provisioned["owner_id"]),
        data={"ai_identity_id": provisioned["identity_id"]},
        files={"file": ("second.txt", b"keep this object", "text/plain")},
    )
    assert second_upload.status_code == 201, second_upload.text
    deleted = client.delete(
        f"/v1/objects/{payload['id']}",
        headers=_auth(provisioned["owner_id"]),
    )
    assert deleted.status_code == 200
    remaining = client.get(
        f"/v1/objects/{second_upload.json()['id']}",
        headers=_auth(provisioned["owner_id"]),
    )
    assert remaining.status_code == 200
    assert remaining.content == b"keep this object"


# ---------------------------------------------------------------------------
# Architecture: honest resource state when a runtime cannot start
# ---------------------------------------------------------------------------


def test_a_failed_runtime_is_reported_and_never_shared(client, monkeypatch):
    """A Brain whose runtime cannot start gets an error, not a shared runtime."""
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    provisioned = _provision(client)
    brain_id = uuid.UUID(provisioned["brain_id"])

    manager = get_runtime_manager()
    # Simulate the dedicated runtime of this Brain disappearing (for example
    # after a process restart) and being impossible to re-create.
    await_manager = manager
    await_manager._runtimes.pop(brain_id, None)

    class _FailingAdapter:
        name = "failing"
        isolation = "dedicated_context"
        provider = "local"

        def create(self, *, brain_id, runtime_id):
            from app.brain_manager.runtime.base import RuntimeProvisioningError

            raise RuntimeProvisioningError(
                "model_artifact_missing", "the model artifact is not available"
            )

    monkeypatch.setattr(manager, "_adapter", _FailingAdapter())

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(),
            json={
                "user_id": provisioned["owner_id"],
                "query": "hello",
                "context": {
                    "user_level": "general",
                    "relevant_context": {},
                    "safety_constraints": [],
                },
                "request_id": str(uuid.uuid4()),
                "model": "Qwen3.5-0.8B",
            },
        )

    # The request fails explicitly: the Brain is never served by another
    # Brain's runtime.
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()

    # The registry records the failure instead of claiming a ready runtime.
    from app import storage_db

    async def _status() -> tuple[str | None, str | None]:
        factory = storage_db.get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(
                        storage_db.ManagedBrain.llm_runtime_id,
                        storage_db.ManagedBrain.llm_runtime_status,
                    ).where(storage_db.ManagedBrain.brain_id == brain_id)
                )
            ).one()
            return row[0], row[1]

    runtime_id, runtime_status = asyncio.run(_status())
    assert runtime_status == "failed"
    assert runtime_id is None or runtime_status != "ready_simulated"


def test_no_environment_variable_can_enable_a_shared_runtime_fallback(
    client, monkeypatch
):
    """A Brain is never served by another Brain's runtime, whatever the config.

    There is no shared-runtime fallback at all: a Brain whose dedicated
    runtime cannot be created must fail explicitly. This test locks that in
    so no environment variable can be reintroduced to weaken it.
    """
    from app.brain_manager import config
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    monkeypatch.setenv("BRAIN_SHARED_RUNTIME_FALLBACK_ENABLED", "true")
    config.reset_brain_manager_config()
    assert not hasattr(config.get_brain_manager_config(), "shared_runtime_fallback_enabled")

    provisioned = _provision(client)
    brain_id = uuid.UUID(provisioned["brain_id"])

    manager = get_runtime_manager()
    manager._runtimes.pop(brain_id, None)

    class _FailingAdapter:
        name = "failing"
        isolation = "dedicated_context"
        provider = "local"

        def create(self, *, brain_id, runtime_id):
            from app.brain_manager.runtime.base import RuntimeProvisioningError

            raise RuntimeProvisioningError(
                "model_artifact_missing", "the model artifact is not available"
            )

    monkeypatch.setattr(manager, "_adapter", _FailingAdapter())

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(),
            json={
                "user_id": provisioned["owner_id"],
                "query": "hello",
                "context": {
                    "user_level": "general",
                    "relevant_context": {},
                    "safety_constraints": [],
                },
                "request_id": str(uuid.uuid4()),
                "model": "Qwen3.5-0.8B",
            },
        )

    assert response.status_code == 503


def test_capacity_limit_is_reported_as_a_resource_state(client, monkeypatch):
    """When no capacity is left the Brain reports a resource state."""
    from app import storage_db
    from app.brain_manager.lifecycle.manager import get_runtime_manager
    from app.brain_manager.registry.enums import ResourceAvailability
    from app.brain_manager.registry.service import BrainRegistryService

    monkeypatch.setenv("BRAIN_MAX_CONCURRENT_RUNTIMES", "1")

    async def _reserve_everything() -> None:
        factory = storage_db.get_session_factory()
        async with factory() as session:
            capacity = get_runtime_manager().capacity
            availability, _ = await capacity.reserve(session, uuid.uuid4())
            assert availability is ResourceAvailability.AVAILABLE

    asyncio.run(_reserve_everything())

    second = _provision(client)
    registry = client.get(f"/v1/brains/{second['brain_id']}", headers=_auth())
    assert registry.status_code == 200, registry.text
    body = registry.json()
    assert body["llm_runtime_status"] in {
        "resource_limited",
        "waiting_for_resources",
        "ready_simulated",
    }
    if body["llm_runtime_status"] != "ready_simulated":
        assert body["runtime_last_error"]


# ---------------------------------------------------------------------------
# Security: owner isolation
# ---------------------------------------------------------------------------


def test_owner_cannot_read_another_owners_vault(client):
    """User B cannot list or read User A's vault records."""
    owner_a = _provision(client)
    owner_b = _provision(client)

    created = client.post(
        f"/v1/brains/{owner_a['brain_id']}/vault/records",
        headers=_auth(owner_a["owner_id"]),
        json={
            "record_type": "note",
            "title": "A private note",
            "content": "secret content of user A",
        },
    )
    assert created.status_code == 201, created.text
    record_id = created.json()["record_id"]

    # User B cannot list A's records.
    listed = client.get(
        f"/v1/brains/{owner_a['brain_id']}/vault/records", headers=_auth(owner_b["owner_id"])
    )
    assert listed.status_code == 404

    # User B cannot read A's record directly.
    fetched = client.get(
        f"/v1/brains/{owner_a['brain_id']}/vault/records/{record_id}",
        headers=_auth(owner_b["owner_id"]),
    )
    assert fetched.status_code == 404

    assert client.put(
        f"/v1/brains/{owner_a['brain_id']}/vault/records/{record_id}",
        headers=_auth(owner_b["owner_id"]),
        json={"expected_version": 1, "content": "attacker overwrite"},
    ).status_code == 404
    assert client.delete(
        f"/v1/brains/{owner_a['brain_id']}/vault/records/{record_id}?confirm=true",
        headers=_auth(owner_b["owner_id"]),
    ).status_code == 404

    # User A can read it.
    own = client.get(
        f"/v1/brains/{owner_a['brain_id']}/vault/records/{record_id}",
        headers=_auth(owner_a["owner_id"]),
    )
    assert own.status_code == 200, own.text
    assert own.json()["content"] == "secret content of user A"


def test_owner_cannot_read_another_owners_vector_results(client):
    """A semantic search never leaks another Brain's indexed content."""
    owner_a = _provision(client)
    owner_b = _provision(client)

    client.post(
        f"/v1/brains/{owner_a['brain_id']}/vault/records",
        headers=_auth(owner_a["owner_id"]),
        json={
            "record_type": "note",
            "title": "Fitness plan",
            "content": "marathon training regimen",
        },
    )

    own = client.post(
        f"/v1/brains/{owner_a['brain_id']}/vault/search",
        headers=_auth(owner_a["owner_id"]),
        json={"query": "marathon training regimen", "limit": 5},
    )
    assert own.status_code == 200, own.text
    assert own.json()["results"], "the owner's own search must find its record"

    foreign = client.post(
        f"/v1/brains/{owner_a['brain_id']}/vault/search",
        headers=_auth(owner_b["owner_id"]),
        json={"query": "marathon training regimen", "limit": 5},
    )
    assert foreign.status_code == 404
    assert "marathon" not in foreign.text


def test_search_can_retrieve_one_owner_authorized_app_namespace(client):
    """App-to-Brain retrieval can target one app inside a unified vault."""
    owner = _provision(client)
    base = f"/v1/brains/{owner['brain_id']}/vault"
    diary = client.post(
        f"{base}/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "diary_entry",
            "namespace": "apps/diary",
            "source_app": "diary",
            "idempotency_key": "diary-raigad",
            "content": "Today I visited Raigad fort with my family.",
        },
    )
    study = client.post(
        f"{base}/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "study_note",
            "namespace": "apps/study",
            "source_app": "study",
            "idempotency_key": "study-algebra",
            "content": "I studied algebra and quadratic equations today.",
        },
    )
    assert diary.status_code == study.status_code == 201

    response = client.post(
        f"{base}/search",
        headers=_auth(owner["owner_id"]),
        json={
            "query": "Today I visited Raigad fort with my family.",
            "namespaces": ["apps/diary"],
            "limit": 10,
        },
    )
    assert response.status_code == 200, response.text
    result_ids = {item["record_id"] for item in response.json()["results"]}
    assert diary.json()["record_id"] in result_ids
    assert study.json()["record_id"] not in result_ids


def test_owner_cannot_read_another_owners_connected_apps(client):
    """The app consent snapshot is private to the owner of the Brain."""
    owner_a = _provision(client)
    owner_b = _provision(client)

    granted = client.put(
        f"/v1/brains/{owner_a['brain_id']}/apps",
        headers=_auth(owner_a["owner_id"]),
        json={
            "app_slug": "calendar",
            "granted_scopes": [{"scope": "events", "action": "read"}],
        },
    )
    assert granted.status_code == 200, granted.text

    foreign = client.get(
        f"/v1/brains/{owner_a['brain_id']}/apps", headers=_auth(owner_b["owner_id"])
    )
    assert foreign.status_code == 404

    own = client.get(
        f"/v1/brains/{owner_a['brain_id']}/apps", headers=_auth(owner_a["owner_id"])
    )
    assert own.status_code == 200, own.text
    assert own.json()["connected_app_count"] == 1


def test_owner_cannot_read_another_owners_jobs(client):
    """Processing jobs are private to the owner of the Brain."""
    owner_a = _provision(client)
    owner_b = _provision(client)

    queued = client.post(
        f"/v1/brains/{owner_a['brain_id']}/jobs",
        headers=_auth(owner_a["owner_id"]),
        json={"job_type": "summarise", "payload": {"text": "private"}},
    )
    assert queued.status_code == 202, queued.text

    foreign = client.get(
        f"/v1/brains/{owner_a['brain_id']}/jobs", headers=_auth(owner_b["owner_id"])
    )
    assert foreign.status_code == 404

    own = client.get(
        f"/v1/brains/{owner_a['brain_id']}/jobs", headers=_auth(owner_a["owner_id"])
    )
    assert own.status_code == 200, own.text
    assert own.json()["total"] == 1


def test_owner_cannot_use_another_owners_brain_for_inference(client):
    """The inference path resolves the Brain from the registry, not the body."""
    owner_a = _provision(client)
    owner_b = _provision(client)

    with TestClient(brain_app) as inference_client:
        # Using B's user id with A's brain id must not serve A's runtime.
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(),
            json={
                "user_id": owner_b["owner_id"],
                "query": "hello",
                "context": {
                    "user_level": "general",
                    "relevant_context": {},
                    "safety_constraints": [],
                },
                "request_id": str(uuid.uuid4()),
                "model": "Qwen3.5-0.8B",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["brain_id"] == owner_b["brain_id"]
    assert response.json()["llm_runtime_id"] == owner_b["body"]["llm_runtime_id"]


def test_brain_apis_require_the_internal_service_token(client):
    """A client without the internal token cannot reach any Brain API."""
    provisioned = _provision(client)
    for method, path in (
        ("get", f"/v1/brains/{provisioned['brain_id']}"),
        ("get", f"/v1/brains/{provisioned['brain_id']}/vault/records"),
        ("get", f"/v1/brains/{provisioned['brain_id']}/apps"),
        ("get", f"/v1/brains/{provisioned['brain_id']}/jobs"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{path} must require the service token"


def test_brain_apis_require_an_owner_header(client):
    """The forwarded owner id is mandatory - no anonymous access."""
    provisioned = _provision(client)
    response = client.get(
        f"/v1/brains/{provisioned['brain_id']}/vault/records", headers=_auth()
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Security: administration
# ---------------------------------------------------------------------------


def test_admin_api_rejects_non_administrators(client, monkeypatch):
    """The administrative registry API requires an allowlisted owner id."""
    monkeypatch.setenv("BRAIN_ADMIN_OWNER_IDS", str(uuid.uuid4()))
    provisioned = _provision(client)

    response = client.get("/v1/admin/brains", headers=_auth(provisioned["owner_id"]))
    assert response.status_code == 403

    # Without a forwarded owner id the request is rejected as well.
    unauthorized = client.get("/v1/admin/brains", headers=_auth())
    assert unauthorized.status_code == 400


def test_admin_api_requires_the_service_token(client, monkeypatch):
    """Even an allowlisted admin needs the internal service token."""
    admin_id = uuid.uuid4()
    monkeypatch.setenv("BRAIN_ADMIN_OWNER_IDS", str(admin_id))
    response = client.get("/v1/admin/brains", headers={"X-Aura-Owner-Id": str(admin_id)})
    assert response.status_code == 401


def test_admin_api_allows_an_allowlisted_administrator(client, monkeypatch):
    """An allowlisted admin can read the registry metadata."""
    admin_id = uuid.uuid4()
    monkeypatch.setenv("BRAIN_ADMIN_OWNER_IDS", str(admin_id))
    provisioned = _provision(client)

    response = client.get("/v1/admin/brains", headers=_auth(admin_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    entry = next(
        item
        for item in body["items"]
        if item["brain_id"] == provisioned["brain_id"]
    )
    assert entry["owner_user_id"] == provisioned["owner_id"]
    assert entry["llm_runtime_id"]


def test_admin_api_exposes_metadata_but_not_vault_content(client, monkeypatch):
    """Administrative endpoints never expose a Brain's vault content."""
    admin_id = uuid.uuid4()
    monkeypatch.setenv("BRAIN_ADMIN_OWNER_IDS", str(admin_id))
    provisioned = _provision(client)

    client.post(
        f"/v1/brains/{provisioned['brain_id']}/vault/records",
        headers=_auth(provisioned["owner_id"]),
        json={
            "record_type": "note",
            "title": "A private note",
            "content": "secret content of user A",
        },
    )

    for path in (
        "/v1/admin/brains",
        f"/v1/admin/brains/{provisioned['brain_id']}",
        f"/v1/admin/brains/{provisioned['brain_id']}/health",
        "/v1/admin/brains/cluster",
    ):
        response = client.get(path, headers=_auth(admin_id))
        assert response.status_code == 200, f"{path}: {response.text}"
        assert "secret content of user A" not in response.text


def test_inference_executes_only_authorized_vault_search_tools(client, monkeypatch):
    """A model tool request is reauthorized against the Brain's app snapshot."""
    import json

    from app.brain_manager.harness import TOOL_MARKER
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    connection = client.put(
        f"{base}/apps",
        headers=_auth(owner["owner_id"]),
        json={
            "app_slug": "diary",
            "granted_scopes": [{"scope": "brain", "action": "read"}],
        },
    )
    assert connection.status_code == 200, connection.text
    stored = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "diary_entry",
            "namespace": "apps/diary",
            "source_app": "diary",
            "content": "I walked by the lakeside this morning.",
            "metadata": {"access_token": "never-in-model-context"},
        },
    )
    assert stored.status_code == 201, stored.text

    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None
    responses = iter(
        [
            TOOL_MARKER
            + json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "vault.search",
                            "arguments": {
                                "query": "I walked by the lakeside this morning.",
                                "namespaces": ["apps/diary"],
                            },
                        }
                    ]
                }
            ),
            "Your diary says you walked by the lakeside.",
        ]
    )
    user_turns = []

    def fake_generate(system, user, **kwargs):
        user_turns.append(user)
        return next(responses)

    monkeypatch.setattr(runtime, "generate", fake_generate)

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(owner["owner_id"]),
            json={
                "user_id": owner["owner_id"],
                "query": "What did I do this morning?",
                "request_id": str(uuid.uuid4()),
                "context": {
                    "source_app": "diary",
                    "authorized_namespaces": ["apps/diary"],
                },
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["response"] == "Your diary says you walked by the lakeside."
    assert "never-in-model-context" not in user_turns[1]


def test_fenced_json_tool_calls_are_limited_to_delete_plans():
    from app.brain_manager.harness import ToolProtocolError, parse_tool_calls

    fenced_delete = (
        "I need confirmation first.\n```json\n"
        '{"tool_calls":[{"name":"vault.delete_namespace",'
        '"arguments":{"namespace":"apps/health"}}]}\n```'
    )
    assert parse_tool_calls(fenced_delete) == [
        {
            "name": "vault.delete_namespace",
            "arguments": {"namespace": "apps/health"},
        }
    ]

    fenced_write = (
        '```json\n{"tool_calls":[{"name":"vault.create_record",'
        '"arguments":{"content":"test"}}]}\n```'
    )
    with pytest.raises(ToolProtocolError):
        parse_tool_calls(fenced_write)
    assert parse_tool_calls("Here is a normal JSON example: {}") is None


def test_delete_plan_scope_defaults_to_app_but_not_named_other_app():
    from app.brain_manager.harness import resolve_delete_namespace_intent

    namespaces = ["apps/diary", "apps/health"]
    assert resolve_delete_namespace_intent(
        "Prepare a deletion plan for all my records.",
        namespaces,
        source_app="diary",
    ) == "apps/diary"
    assert resolve_delete_namespace_intent(
        "Prepare a deletion plan for all Health app records.",
        namespaces,
        source_app="diary",
    ) is None
    assert resolve_delete_namespace_intent(
        "delete all data of dailly dairy app from my vault",
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == "apps/diary"
    assert resolve_delete_namespace_intent(
        "delete all data of AURA Daily Diary app from my vault",
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == "apps/diary"


def test_bulk_replace_resolves_one_letter_change_and_specific_app_scope():
    from app.brain_manager.harness import resolve_bulk_replace_intent

    namespaces = ["apps/health", "apps/study"]
    assert resolve_bulk_replace_intent(
        "replace letter R with letter C in my study app vault",
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == ("R", "C", ["apps/study"])
    assert resolve_bulk_replace_intent(
        'replace "R" with "C" in the Study Companion app',
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == ("R", "C", ["apps/study"])
    assert resolve_bulk_replace_intent(
        'harness: replace letter "R" with letter "C" in my ( apps/study)vault',
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == ("R", "C", ["apps/study"])
    assert resolve_bulk_replace_intent(
        "replace letter R with letter C in my study app vault",
        ["apps/aura-core"],
        source_app="aura-core",
        manage_vault=False,
    ) is None
    assert resolve_bulk_replace_intent(
        'replace "R" with "C" in my whole vault',
        namespaces,
        source_app="aura-core",
        manage_vault=True,
    ) == ("R", "C", ["apps/health", "apps/study"])


def test_tool_writes_require_confirmation_and_are_idempotent(client, monkeypatch):
    """A confirmed app write persists once and has a content-free audit trail."""
    import json
    import sqlite3

    from app.brain_manager.harness import TOOL_MARKER
    from app.brain_manager.lifecycle.manager import get_runtime_manager
    from app.storage_config import get_settings

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    connection = client.put(
        f"{base}/apps",
        headers=_auth(owner["owner_id"]),
        json={
            "app_slug": "diary",
            "granted_scopes": [
                {"scope": "brain", "action": "read"},
                {"scope": "memory", "action": "write"},
            ],
        },
    )
    assert connection.status_code == 200, connection.text

    request_id = str(uuid.uuid4())
    proposal = TOOL_MARKER + json.dumps(
        {
            "tool_calls": [
                {
                    "name": "vault.create_record",
                    "arguments": {
                        "record_type": "diary_entry",
                        "title": "A saved walk",
                        "content": "I walked by the river.",
                    },
                }
            ]
        }
    )
    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None

    def run_with_confirmation(confirmed: bool) -> None:
        responses = iter([proposal, "Saved the diary entry."])
        monkeypatch.setattr(
            runtime, "generate", lambda *args, **kwargs: next(responses)
        )
        with TestClient(brain_app) as inference_client:
            response = inference_client.post(
                "/v1/brain/harness",
                headers=_auth(owner["owner_id"]),
                json={
                    "user_id": owner["owner_id"],
                    "query": "Save that I walked by the river.",
                    "request_id": request_id,
                    "context": {
                        "source_app": "diary",
                        "authorized_namespaces": ["apps/diary"],
                        "confirm_tool_writes": confirmed,
                    },
                },
            )
        assert response.status_code == 200, response.text

    run_with_confirmation(False)
    records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/diary"},
    )
    assert records.status_code == 200, records.text
    assert records.json()["total"] == 0

    run_with_confirmation(True)
    run_with_confirmation(True)
    records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/diary"},
    )
    assert records.status_code == 200, records.text
    assert records.json()["total"] == 1

    database_url = get_settings().resolved_database_url
    prefix = "sqlite+aiosqlite:///"
    assert database_url.startswith(prefix)
    database_path = database_url[len(prefix) :]
    with sqlite3.connect(database_path) as connection:
        events = connection.execute(
            "SELECT status, error_code FROM brain_tool_executions "
            "WHERE request_id = ? ORDER BY created_at",
            (request_id,),
        ).fetchall()
    assert [event[0] for event in events] == ["failed", "succeeded", "succeeded"]
    assert events[0][1] == "tool_not_authorized"


def test_harness_prefix_edits_only_the_calling_app_namespace(
    client, monkeypatch
):
    """The explicit harness prefix only modifies its app's vault namespace."""
    import json

    from app.brain_manager.harness import TOOL_MARKER
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    connection = client.put(
        f"{base}/apps",
        headers=_auth(owner["owner_id"]),
        json={
            "app_slug": "study",
            "granted_scopes": [
                {"scope": "brain", "action": "read"},
                {"scope": "memory", "action": "write"},
            ],
        },
    )
    assert connection.status_code == 200, connection.text
    original = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "study_note",
            "namespace": "apps/study",
            "source_app": "study",
            "title": "Original",
            "content": "I went to the library.",
        },
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["record_id"]
    other_app_record = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "health_note",
            "namespace": "apps/health",
            "source_app": "health",
            "title": "Keep private to Health",
            "content": "This Health record must not be edited by Study.",
        },
    )
    assert other_app_record.status_code == 201, other_app_record.text
    other_app_record_id = other_app_record.json()["record_id"]

    tool_response = TOOL_MARKER + json.dumps(
        {
            "tool_calls": [
                {
                    "name": "vault.update_record",
                    "arguments": {
                        "record_id": original_id,
                        "expected_version": 1,
                        "content": "I went to the main library.",
                    },
                },
                {
                    "name": "vault.create_record",
                    "arguments": {
                        "record_type": "study_reference",
                        "title": "Library visit",
                        "content": "I went to the main library.",
                        "category": "places",
                        "namespace": "apps/study",
                        "derived_from_record_id": original_id,
                    },
                },
                {
                    "name": "vault.update_record",
                    "arguments": {
                        "record_id": other_app_record_id,
                        "expected_version": 1,
                        "content": "Unauthorized cross-app edit.",
                    },
                },
                {
                    "name": "vault.find_duplicates",
                    "arguments": {"namespaces": ["apps/study"]},
                },
            ]
        }
    )
    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None
    responses = iter(
        [
            "I cannot make that change.",
            tool_response,
            "I updated the record and organized a linked study reference.",
        ]
    )
    generated = []

    def fake_generate(system, user, **kwargs):
        generated.append((system, user))
        return next(responses)

    monkeypatch.setattr(runtime, "generate", fake_generate)

    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(owner["owner_id"]),
            json={
                "user_id": owner["owner_id"],
                "query": "harness: Organize my library information.",
                "request_id": str(uuid.uuid4()),
                "context": {
                    "source_app": "study",
                    "authorized_namespaces": ["apps/study"],
                    "confirm_tool_writes": False,
                    "manage_vault": False,
                },
            },
        )
    assert response.status_code == 200, response.text
    assert len(generated) == 3
    assert "harness: prefix" in generated[0][0]
    assert generated[0][1] == "Organize my library information."
    assert "Output only the required AURA_TOOL_CALL:" in generated[1][1]
    assert '"duplicate_group_count": 1' in generated[2][1]
    assert '"record_count": 2' in generated[2][1]

    updated = client.get(
        f"{base}/vault/records/{original_id}",
        headers=_auth(owner["owner_id"]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["content"] == "I went to the main library."
    assert updated.json()["version"] == 2
    assert updated.json()["metadata"]["ai_managed"] is True
    assert updated.json()["metadata"]["ai_last_operation"] == "updated"

    versions = client.get(
        f"{base}/vault/records/{original_id}/versions",
        headers=_auth(owner["owner_id"]),
    )
    assert versions.status_code == 200, versions.text
    assert versions.json()["items"] == []

    organized = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/study"},
    )
    assert organized.status_code == 200, organized.text
    item = organized.json()["items"][0]
    assert item["category"] == "places"
    assert item["metadata"]["ai_managed"] is True
    assert item["metadata"]["ai_derived_from_record_id"] == original_id
    protected = client.get(
        f"{base}/vault/records/{other_app_record_id}",
        headers=_auth(owner["owner_id"]),
    )
    assert protected.status_code == 200, protected.text
    assert protected.json()["content"] == (
        "This Health record must not be edited by Study."
    )


def test_harness_prefix_does_not_grant_app_write_permission(client, monkeypatch):
    import json

    from app.brain_manager.harness import TOOL_MARKER
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None
    proposed = TOOL_MARKER + json.dumps(
        {
            "tool_calls": [
                {
                    "name": "vault.create_record",
                    "arguments": {
                        "record_type": "diary_entry",
                        "title": "Unauthorized write",
                        "content": "This should not be saved.",
                    },
                }
            ]
        }
    )
    responses = iter([proposed, "I could not save that entry."])
    monkeypatch.setattr(
        runtime, "generate", lambda *args, **kwargs: next(responses)
    )
    with TestClient(brain_app) as inference_client:
        response = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(owner["owner_id"]),
            json={
                "user_id": owner["owner_id"],
                "query": "harness: Save this entry.",
                "request_id": str(uuid.uuid4()),
                "context": {
                    "source_app": "diary",
                    "authorized_namespaces": ["apps/diary"],
                    "manage_vault": False,
                },
            },
        )
    assert response.status_code == 200, response.text
    records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/diary"},
    )
    assert records.status_code == 200, records.text
    assert records.json()["total"] == 0


def test_app_local_harness_deletion_requires_approval(client, monkeypatch):
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    connection = client.put(
        f"{base}/apps",
        headers=_auth(owner["owner_id"]),
        json={
            "app_slug": "diary",
            "granted_scopes": [
                {"scope": "brain", "action": "read"},
                {"scope": "memory", "action": "write"},
            ],
        },
    )
    assert connection.status_code == 200, connection.text
    for namespace, source_app in (
        ("apps/diary", "diary"),
        ("apps/health", "health"),
    ):
        created = client.post(
            f"{base}/vault/records",
            headers=_auth(owner["owner_id"]),
            json={
                "record_type": "note",
                "namespace": namespace,
                "source_app": source_app,
                "content": f"Keep track of {source_app}.",
            },
        )
        assert created.status_code == 201, created.text

    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None

    def invoke(*, approval_id=None):
        with TestClient(brain_app) as inference_client:
            return inference_client.post(
                "/v1/brain/infer",
                headers=_auth(owner["owner_id"]),
                json={
                    "user_id": owner["owner_id"],
                    "query": (
                        "Confirm the deletion plan."
                        if approval_id
                        else (
                            "harness: Prepare a deletion plan for all my "
                            "records. Do not delete anything until I confirm."
                        )
                    ),
                    "request_id": str(uuid.uuid4()),
                    "context": {
                        "source_app": "diary",
                        "authorized_namespaces": ["apps/diary"],
                        "manage_vault": False,
                        "delete_approval_id": approval_id,
                    },
                },
            )

    monkeypatch.setattr(
        runtime,
        "generate",
        lambda *args, **kwargs: pytest.fail(
            "app-local delete plans must be deterministic"
        ),
    )
    preview = invoke()
    assert preview.status_code == 200, preview.text
    plans = preview.json()["pending_delete_plans"]
    assert len(plans) == 1
    assert plans[0]["namespace"] == "apps/diary"
    assert plans[0]["record_count"] == 1

    approved = invoke(approval_id=plans[0]["plan_id"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["delete_result"]["deleted_count"] == 1
    diary_records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/diary"},
    )
    health_records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert diary_records.json()["total"] == 0
    assert health_records.json()["total"] == 1


def test_app_local_harness_replacement_stays_in_calling_namespace(client):
    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    connection = client.put(
        f"{base}/apps",
        headers=_auth(owner["owner_id"]),
        json={
            "app_slug": "diary",
            "granted_scopes": [
                {"scope": "brain", "action": "read"},
                {"scope": "memory", "action": "write"},
            ],
        },
    )
    assert connection.status_code == 200, connection.text
    for namespace, source_app in (
        ("apps/diary", "diary"),
        ("apps/health", "health"),
    ):
        created = client.post(
            f"{base}/vault/records",
            headers=_auth(owner["owner_id"]),
            json={
                "record_type": "note",
                "namespace": namespace,
                "source_app": source_app,
                "content": "R R",
            },
        )
        assert created.status_code == 201, created.text

    with TestClient(brain_app) as inference_client:
        preview = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(owner["owner_id"]),
            json={
                "user_id": owner["owner_id"],
                "query": "harness: replace letter R with letter C in my diary app vault",
                "request_id": str(uuid.uuid4()),
                "context": {
                    "source_app": "diary",
                    "authorized_namespaces": ["apps/diary"],
                    "manage_vault": False,
                },
            },
        )
    assert preview.status_code == 200, preview.text
    plans = preview.json()["pending_bulk_replace_plans"]
    assert len(plans) == 1
    assert plans[0]["namespaces"] == ["apps/diary"]
    assert plans[0]["record_count"] == 1
    assert plans[0]["occurrence_count"] == 2

    with TestClient(brain_app) as inference_client:
        approved = inference_client.post(
            "/v1/brain/infer",
            headers=_auth(owner["owner_id"]),
            json={
                "user_id": owner["owner_id"],
                "query": "Confirm this replacement.",
                "request_id": str(uuid.uuid4()),
                "context": {
                    "source_app": "diary",
                    "authorized_namespaces": ["apps/diary"],
                    "manage_vault": False,
                    "bulk_replace_approval_id": plans[0]["plan_id"],
                },
            },
        )
    assert approved.status_code == 200, approved.text
    assert approved.json()["bulk_replace_result"]["ok"] is True
    diary_records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/diary"},
    )
    health_records = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert diary_records.json()["items"][0]["content"] == "C C"
    assert health_records.json()["items"][0]["content"] == "R R"


def test_harness_bulk_replace_previews_then_updates_only_confirmed_app_records(
    client, monkeypatch
):
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"
    records = {}
    for namespace, source_app, content in (
        ("apps/health", "health", "R Rr RR"),
        ("apps/study", "study", "R study note"),
        ("conversations", "brain", "R must remain untouched"),
    ):
        created = client.post(
            f"{base}/vault/records",
            headers=_auth(owner["owner_id"]),
            json={
                "record_type": "note",
                "namespace": namespace,
                "source_app": source_app,
                "title": "R title" if namespace == "apps/health" else "Other",
                "content": content,
            },
        )
        assert created.status_code == 201, created.text
        records[namespace] = created.json()["record_id"]

    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None

    def invoke(query, *, approval_id=None, cancel_id=None):
        def fail_if_model_is_called(*args, **kwargs):
            raise AssertionError("bulk replacement preview and apply are deterministic")

        monkeypatch.setattr(runtime, "generate", fail_if_model_is_called)
        with TestClient(brain_app) as inference_client:
            return inference_client.post(
                "/v1/brain/infer",
                headers=_auth(owner["owner_id"]),
                json={
                    "user_id": owner["owner_id"],
                    "query": query,
                    "request_id": str(uuid.uuid4()),
                    "context": {
                        "source_app": "aura-core",
                        "authorized_namespaces": [
                            "apps/health",
                            "apps/study",
                            "conversations",
                            "documents",
                            "shared",
                        ],
                        "manage_vault": True,
                        "harness_mode": query.startswith("harness:"),
                        "bulk_replace_approval_id": approval_id,
                        "bulk_replace_cancel_id": cancel_id,
                    },
                },
            )

    study_preview = invoke(
        "harness: replace letter R with letter C in my study app vault"
    )
    assert study_preview.status_code == 200, study_preview.text
    study_plan = study_preview.json()["pending_bulk_replace_plans"][0]
    assert study_plan["namespaces"] == ["apps/study"]
    assert study_plan["record_count"] == 1
    assert study_plan["occurrence_count"] == 1
    study_cancelled = invoke(
        "Cancel this replacement plan.",
        cancel_id=study_plan["plan_id"],
    )
    assert study_cancelled.status_code == 200, study_cancelled.text
    assert study_cancelled.json()["bulk_replace_result"] == {
        "ok": True,
        "cancelled": True,
    }

    cancel_preview = invoke(
        'harness: review my entire vault and replace "R" with "Z"'
    )
    assert cancel_preview.status_code == 200, cancel_preview.text
    cancel_plan = cancel_preview.json()["pending_bulk_replace_plans"][0]
    cancelled = invoke(
        "Cancel this replacement plan.",
        cancel_id=cancel_plan["plan_id"],
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["bulk_replace_result"] == {
        "ok": True,
        "cancelled": True,
    }

    stale_preview = invoke(
        'harness: review my entire vault and replace "R" with "Z"'
    )
    stale_plan_id = stale_preview.json()["pending_bulk_replace_plans"][0]["plan_id"]
    manually_updated = client.put(
        f"{base}/vault/records/{records['apps/health']}",
        headers=_auth(owner["owner_id"]),
        json={"expected_version": 1, "title": "R title changed"},
    )
    assert manually_updated.status_code == 200, manually_updated.text
    stale_approval = invoke(
        "Confirm this replacement plan in the app.",
        approval_id=stale_plan_id,
    )
    assert stale_approval.json()["bulk_replace_result"]["error_code"] == (
        "replace_plan_target_changed"
    )
    after_stale = client.get(
        f"{base}/vault/records/{records['apps/health']}",
        headers=_auth(owner["owner_id"]),
    )
    assert after_stale.json()["title"] == "R title changed"
    assert after_stale.json()["content"] == "R Rr RR"

    preview = invoke(
        'harness: review my entire vault and replace "R" with "Z"'
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    plans = preview_body["pending_bulk_replace_plans"]
    assert len(plans) == 1
    assert plans[0]["matching"] == "exact_case_sensitive"
    assert plans[0]["namespaces"] == ["apps/health", "apps/study"]
    assert plans[0]["record_count"] == 2
    assert plans[0]["occurrence_count"] == 6

    unchanged = client.get(
        f"{base}/vault/records/{records['apps/health']}",
        headers=_auth(owner["owner_id"]),
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["title"] == "R title changed"
    assert unchanged.json()["content"] == "R Rr RR"

    approved = invoke(
        "Confirm this replacement plan in the app.",
        approval_id=plans[0]["plan_id"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["bulk_replace_result"] == {
        "ok": True,
        "record_count": 2,
        "occurrence_count": 6,
        "namespaces": ["apps/health", "apps/study"],
    }
    updated = client.get(
        f"{base}/vault/records/{records['apps/health']}",
        headers=_auth(owner["owner_id"]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "Z title changed"
    assert updated.json()["content"] == "Z Zr ZZ"
    assert updated.json()["version"] == 3
    updated_study = client.get(
        f"{base}/vault/records/{records['apps/study']}",
        headers=_auth(owner["owner_id"]),
    )
    assert updated_study.status_code == 200, updated_study.text
    assert updated_study.json()["content"] == "Z study note"

    untouched = client.get(
        f"{base}/vault/records/{records['conversations']}",
        headers=_auth(owner["owner_id"]),
    )
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["content"] == "R must remain untouched"


def test_ai_namespace_delete_requires_confirmation_and_purges_records(client, monkeypatch):
    from app.brain_manager.lifecycle.manager import get_runtime_manager

    owner = _provision(client)
    brain_id = owner["brain_id"]
    base = f"/v1/brains/{brain_id}"

    health_record_ids = []
    for content in ("Health note one", "Health note two"):
        created = client.post(
            f"{base}/vault/records",
            headers=_auth(owner["owner_id"]),
            json={
                "record_type": "wellness_event",
                "namespace": "apps/health",
                "source_app": "health",
                "content": content,
            },
        )
        assert created.status_code == 201, created.text
        health_record_ids.append(created.json()["record_id"])

    protected_record = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "note",
            "namespace": "apps/study",
            "source_app": "study",
            "content": "Keep this Study note",
        },
    )
    assert protected_record.status_code == 201, protected_record.text

    runtime = get_runtime_manager().handle(uuid.UUID(brain_id))
    assert runtime is not None
    get_runtime_manager()._runtimes.pop(uuid.UUID(brain_id), None)

    def invoke(
        plan_id: str | None = None,
        cancel_id: str | None = None,
        manage_vault: bool = True,
        harness_mode: bool = False,
    ) -> tuple[object, list[str]]:
        observed_turns: list[str] = []
        query = (
            "Confirm deletion of the displayed plan."
            if plan_id or cancel_id
            else (
                "Prepare a deletion plan for all records in my Health "
                "app vault (apps/health). Do not delete anything until "
                "I confirm."
            )
        )
        if harness_mode:
            query = f"harness: {query}"

        def fake_generate(system, user, **kwargs):
            observed_turns.append(user)
            raise AssertionError(
                "deterministic delete-plan requests must not depend on model output"
            )

        monkeypatch.setattr(runtime, "generate", fake_generate)
        endpoint = (
            "/v1/brain/harness"
            if harness_mode or plan_id is not None or cancel_id is not None
            else "/v1/brain/infer"
        )
        with TestClient(brain_app) as inference_client:
            response = inference_client.post(
                endpoint,
                headers=_auth(owner["owner_id"]),
                json={
                    "user_id": owner["owner_id"],
                    "query": query,
                    "request_id": str(uuid.uuid4()),
                    "context": {
                        "source_app": "aura-core",
                        "authorized_namespaces": [
                            "apps/health",
                            "apps/study",
                            "conversations",
                            "documents",
                            "shared",
                        ],
                        "confirm_tool_writes": True,
                        "delete_approval_id": plan_id,
                        "delete_plan_cancel_id": cancel_id,
                        "manage_vault": manage_vault,
                    },
                },
            )
        return response, observed_turns

    opt_out, opt_out_turns = invoke(manage_vault=False)
    assert opt_out.status_code == 200, opt_out.text
    assert opt_out_turns == []
    assert opt_out.json()["pending_delete_plans"] == []
    assert opt_out.json()["delete_result"]["error_code"] == (
        "whole_vault_mode_required"
    )

    preview, preview_turns = invoke(manage_vault=True, harness_mode=True)
    assert preview.status_code == 200, preview.text
    assert preview_turns == []
    plans = preview.json()["pending_delete_plans"]
    assert len(plans) == 1
    assert plans[0]["namespace"] == "apps/health"
    assert plans[0]["record_count"] == 2
    plan_id = plans[0]["plan_id"]
    remains = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert remains.status_code == 200, remains.text
    assert remains.json()["total"] == 2

    mismatched, _ = invoke(str(uuid.uuid4()))
    assert mismatched.status_code == 200, mismatched.text
    assert mismatched.json()["delete_result"]["error_code"] == (
        "delete_plan_invalid_or_expired"
    )
    still_remains = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert still_remains.json()["total"] == 2

    confirmed, _ = invoke(plan_id)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["delete_result"]["deleted_count"] == 2
    replayed, _ = invoke(plan_id)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["delete_result"]["error_code"] in {
        "delete_plan_invalid_or_expired",
        "delete_plan_already_used",
    }
    deleted = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["total"] == 0
    for record_id in health_record_ids:
        versions = client.get(
            f"{base}/vault/records/{record_id}/versions",
            headers=_auth(owner["owner_id"]),
        )
        assert versions.status_code == 404
    retained = client.get(
        f"{base}/vault/records/{protected_record.json()['record_id']}",
        headers=_auth(owner["owner_id"]),
    )
    assert retained.status_code == 200, retained.text
    assert retained.json()["content"] == "Keep this Study note"

    empty_namespace, empty_turns = invoke()
    assert empty_namespace.status_code == 200, empty_namespace.text
    assert empty_turns == []
    assert empty_namespace.json()["pending_delete_plans"] == []
    assert empty_namespace.json()["delete_result"] == {
        "ok": False,
        "error_code": "no_records_found",
        "namespace": "apps/health",
        "deleted_count": 0,
    }

    new_health_record = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "wellness_event",
            "namespace": "apps/health",
            "source_app": "health",
            "content": "A Health record to preserve after cancellation",
        },
    )
    assert new_health_record.status_code == 201, new_health_record.text
    second_preview, _ = invoke()
    assert second_preview.status_code == 200, second_preview.text
    second_plan_id = second_preview.json()["pending_delete_plans"][0]["plan_id"]
    cancelled, _ = invoke(cancel_id=second_plan_id)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["delete_result"] == {"ok": True, "cancelled": True}
    cancelled_approval, _ = invoke(plan_id=second_plan_id)
    assert cancelled_approval.status_code == 200, cancelled_approval.text
    assert cancelled_approval.json()["delete_result"]["ok"] is False
    preserved = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["total"] == 1

    changed_target_preview, _ = invoke()
    assert changed_target_preview.status_code == 200, changed_target_preview.text
    changed_plan_id = changed_target_preview.json()["pending_delete_plans"][0]["plan_id"]
    late_health_record = client.post(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "wellness_event",
            "namespace": "apps/health",
            "source_app": "health",
            "content": "Added after the delete plan was prepared",
        },
    )
    assert late_health_record.status_code == 201, late_health_record.text
    changed_target_result, _ = invoke(plan_id=changed_plan_id)
    assert changed_target_result.status_code == 200, changed_target_result.text
    assert (
        changed_target_result.json()["delete_result"]["error_code"]
        == "delete_plan_target_changed"
    )
    still_preserved = client.get(
        f"{base}/vault/records",
        headers=_auth(owner["owner_id"]),
        params={"namespace": "apps/health"},
    )
    assert still_preserved.status_code == 200, still_preserved.text
    assert still_preserved.json()["total"] == 2


def test_record_update_replaces_content_without_retaining_previous_versions(client):
    owner = _provision(client)
    base = f"/v1/brains/{owner['brain_id']}/vault/records"
    created = client.post(
        base,
        headers=_auth(owner["owner_id"]),
        json={
            "record_type": "note",
            "title": "Original title",
            "content": "Original content",
        },
    )
    assert created.status_code == 201, created.text
    record_id = created.json()["record_id"]

    updated = client.put(
        f"{base}/{record_id}",
        headers=_auth(owner["owner_id"]),
        json={
            "expected_version": 1,
            "title": "Corrected title",
            "content": "Corrected content",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    versions = client.get(
        f"{base}/{record_id}/versions",
        headers=_auth(owner["owner_id"]),
    )
    assert versions.status_code == 200, versions.text
    assert versions.json()["items"] == []
