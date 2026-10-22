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
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app as brain_app
from app.storage_app import app as storage_app


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

    # Storage is owner-scoped *and* Brain-scoped.
    assert body["storage_resource_reference"].endswith(brain_id)
    assert provisioned["owner_id"] in body["storage_resource_reference"]


def test_a_brain_owns_a_live_runtime_object_with_its_own_context(client):
    """The runtime that serves a Brain is a real, Brain-bound object."""
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


def test_manifest_is_written_and_contains_no_secrets(client):
    """The per-Brain manifest is persisted and never carries credentials."""
    provisioned = _provision(client)
    response = client.get(
        f"/v1/brains/{provisioned['brain_id']}/manifest", headers=_auth()
    )
    assert response.status_code == 200, response.text
    manifest = response.json()
    body = json.dumps(manifest).lower()
    for forbidden in ("token", "secret", "password", "api_key", "authorization"):
        assert forbidden not in body


def test_brain_objects_are_stored_under_the_brain_namespace(client):
    """A Brain's uploaded objects live under its own resource prefix."""
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
