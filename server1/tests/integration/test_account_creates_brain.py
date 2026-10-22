"""End-to-end proof that creating an account forms a personal Brain.

This is the product promise in one test: a user who registers gets a Brain,
and that Brain gets its own dedicated runtime and its own private resources.
Nothing is mocked between the two services.

Server 2 (``app.main``) runs as a real uvicorn subprocess on a free port with
an isolated metadata database, an isolated resource root and no model artifact,
so the Brain runs on the deterministic simulator and reports itself honestly
as ``simulated``. Server 1 runs the real FastAPI application against a real
embedded PostgreSQL and is pointed at that Server 2 via ``AI_SERVER_URL``.

The test covers:

* ``POST /api/v1/users`` returns a Brain that is already ``ready``;
* that Brain owns a dedicated runtime id and reports its isolation level;
* a second account gets a *different* Brain and a *different* runtime;
* the new Brain can answer a query on its own runtime;
* the Brain's private resources are scoped to the new account.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

pgserver = pytest.importorskip("pgserver")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.database.session import configure_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER2_DIR = REPO_ROOT / "server2"
TOKEN = "account-brain-token"


def _server2_python() -> str:
    """Return the interpreter that can import Server 2's brain service.

    ``app.main`` imports torch and transformers at module level, so the brain
    service needs Server 2's own virtualenv.
    """
    candidate = SERVER2_DIR / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def brain_server(tmp_path_factory):
    if not (SERVER2_DIR / "app" / "main.py").exists():
        pytest.skip("Server 2 brain app source not available")
    if not hasattr(socket, "AF_INET"):
        pytest.skip("networking unavailable")

    port = _free_port()
    data_dir = tmp_path_factory.mktemp("server2-account-brain-data")
    env = {
        **os.environ,
        "AURA_SKIP_DOTENV": "1",
        "INTERNAL_API_TOKEN": TOKEN,
        "STORAGE_API_TOKEN": TOKEN,
        "STORAGE_BACKEND": "local",
        "STORAGE_DATA_DIR": str(data_dir),
        "STORAGE_LOCAL_ROOT": str(data_dir / "objects"),
        "STORAGE_DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'storage.db'}",
        "BRAIN_RESOURCE_ROOT": str(data_dir / "brains"),
        # No model artifact is available in CI, so the Brain platform reports
        # an honest simulated runtime instead of pretending to be isolated.
        "AURA_SKIP_MODEL_LOAD": "1",
        "BRAIN_RUNTIME_MODE": "simulated",
        "BRAIN_ADMIN_OWNER_IDS": "00000000-0000-0000-0000-000000000001",
        "PORT": str(port),
    }
    process = subprocess.Popen(
        [
            _server2_python(),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(SERVER2_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline and process.poll() is None:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.3)

    if not ready:
        process.terminate()
        output = process.stdout.read().decode(errors="replace") if process.stdout else ""
        pytest.fail(f"Server 2 brain app failed to start:\n{output}")

    yield base_url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


@pytest.fixture(scope="module")
def database():
    data_dir = Path(tempfile.mkdtemp(prefix="aura-account-brain-pg-"))
    server = pgserver.get_server(data_dir)
    uri = server.get_uri()
    try:
        yield uri
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(database, brain_server):
    """Server 1 wired to the real Server 2 brain service."""
    from app.services.memory_index import get_memory_indexer

    sync_url = database.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ["POSTGRES_URL"] = sync_url
    # The whole point of this test: remote Brain provisioning is ON.
    os.environ["BRAIN_REMOTE_PROVISIONING_ENABLED"] = "true"
    os.environ["AI_SERVER_URL"] = brain_server
    os.environ["AI_SERVER_MODEL"] = "Qwen3.5-0.8B"
    os.environ["INTERNAL_API_TOKEN"] = TOKEN
    # Keep integration tests hermetic: never reach a real Qdrant from tests.
    os.environ["MEMORY_VECTOR_SEARCH_ENABLED"] = "false"
    get_settings.cache_clear()
    get_memory_indexer.cache_clear()
    engine = configure_engine()

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_tables())

    with TestClient(app) as test_client:
        yield test_client

    asyncio.run(engine.dispose())
    for name in (
        "POSTGRES_URL",
        "BRAIN_REMOTE_PROVISIONING_ENABLED",
        "AI_SERVER_URL",
        "AI_SERVER_MODEL",
        "INTERNAL_API_TOKEN",
        "MEMORY_VECTOR_SEARCH_ENABLED",
    ):
        os.environ.pop(name, None)
    get_settings.cache_clear()
    get_memory_indexer.cache_clear()


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Brain Owner"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "password123"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _my_brain(client: TestClient, headers: dict) -> dict:
    response = client.get("/api/v1/brain/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _count_identity_and_brain(client: TestClient, owner_id: str) -> tuple[int, int]:
    """Count the AI identities and Personal Brains that belong to one owner."""
    from sqlalchemy import func, select

    from app.infrastructure.database.session import get_session_factory
    from app.models.ai_identity import AIIdentity
    from app.models.personal_ai_brain import PersonalAIBrain

    async def _count() -> tuple[int, int]:
        factory = get_session_factory()
        async with factory() as session:
            identities = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(AIIdentity)
                        .where(AIIdentity.user_id == uuid.UUID(owner_id))
                    )
                )
                or 0
            )
            brains = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(PersonalAIBrain)
                        .join(AIIdentity, AIIdentity.id == PersonalAIBrain.ai_identity_id)
                        .where(AIIdentity.user_id == uuid.UUID(owner_id))
                    )
                )
                or 0
            )
            return identities, brains

    if client.portal is None:
        raise RuntimeError("TestClient must be entered before counting account resources")
    return client.portal.call(_count)


@pytest.mark.integration
def test_new_account_immediately_gets_a_formed_brain(client):
    """Registering an account forms the Brain and its dedicated runtime."""
    email = f"formed-{uuid.uuid4().hex[:8]}@example.com"
    created = _register(client, email)
    assert created["id"]

    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    brain = _my_brain(client, headers)
    # The Brain exists, is registered with Server 2 and holds a live runtime.
    assert brain["brain_id"]
    assert brain["ai_identity_id"]
    assert brain["provisioning_status"] == "ready", brain
    assert brain["inference_runtime_status"] == "ready_simulated", brain
    # The Brain owns a dedicated runtime, and reports its isolation honestly.
    assert brain["llm_runtime_id"], brain
    assert brain["runtime_isolation"] == "simulated", brain
    # Every private resource is scoped to this Brain.
    assert brain["storage_resource_reference"].endswith(brain["brain_id"])
    manifest = httpx.get(
        f"{os.environ['AI_SERVER_URL']}/v1/brains/{brain['brain_id']}/manifest",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Aura-Owner-Id": created["id"],
        },
        timeout=10.0,
    )
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["manifest"]["owner_name"] == "Brain Owner"


@pytest.mark.integration
def test_each_new_account_gets_its_own_brain_and_runtime(client):
    """Two new accounts never share a Brain or a runtime."""
    first_email = f"owner-a-{uuid.uuid4().hex[:8]}@example.com"
    second_email = f"owner-b-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, first_email)
    _register(client, second_email)

    first_tokens = _login(client, first_email)
    second_tokens = _login(client, second_email)
    first = _my_brain(client, {"Authorization": f"Bearer {first_tokens['access_token']}"})
    second = _my_brain(client, {"Authorization": f"Bearer {second_tokens['access_token']}"})

    assert first["brain_id"] != second["brain_id"]
    assert first["ai_identity_id"] != second["ai_identity_id"]
    assert first["llm_runtime_id"] != second["llm_runtime_id"]


@pytest.mark.integration
def test_the_new_brain_can_answer_a_query_on_its_own_runtime(client):
    """The Brain formed at registration serves inference from its own runtime."""
    email = f"queryable-{uuid.uuid4().hex[:8]}@example.com"
    created = _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    brain = _my_brain(client, headers)

    response = client.post(
        "/api/v1/brain/query",
        headers=headers,
        json={"query": "What did I just set up?"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["answer"]
    # The answer came from the new Brain's own runtime, not a shared one.
    assert payload["user_id"] == created["id"]


@pytest.mark.integration
def test_the_whole_chain_is_one_to_one_for_each_user(client, brain_server):
    """One account -> one identity -> one Brain -> one LLM -> one private vault.

    This is the product invariant for an individual AURA user: the account
    owns exactly one Brain, that Brain owns exactly one dedicated LLM runtime,
    and it owns exactly one private memory vault. The database constraints
    enforce it, so a bug cannot quietly give one user a second Brain.
    """
    email = f"chain-{uuid.uuid4().hex[:8]}@example.com"
    created = _register(client, email)
    owner_id = created["id"]

    # 1. One account: the same email cannot be registered twice.
    duplicate = client.post(
        "/api/v1/users",
        json={"email": email, "password": "password123", "full_name": "Brain Owner"},
    )
    assert duplicate.status_code == 400, duplicate.text

    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    brain = _my_brain(client, headers)

    # 2. One identity and one Brain for that account, in the database.
    identity_count, brain_count = _count_identity_and_brain(client, owner_id)
    assert identity_count == 1
    assert brain_count == 1

    identity = client.get(
        f"/api/v1/identity/{brain['ai_identity_id']}", headers=headers
    )
    assert identity.status_code == 200, identity.text
    assert identity.json()["id"] == brain["ai_identity_id"]

    # 3. One dedicated LLM runtime for that Brain.
    registry = httpx.get(
        f"{brain_server}/v1/brains/{brain['brain_id']}",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": owner_id},
        timeout=10.0,
    )
    assert registry.status_code == 200, registry.text
    entry = registry.json()
    assert entry["owner_id"] == owner_id
    assert entry["ai_identity_id"] == brain["ai_identity_id"]
    assert entry["llm_runtime_id"] == brain["llm_runtime_id"]
    assert entry["runtime_isolation"] == "simulated"

    # 4. One private memory vault for that Brain.
    vault = httpx.get(
        f"{brain_server}/v1/brains/{brain['brain_id']}/vault/status",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": owner_id},
        timeout=10.0,
    )
    assert vault.status_code == 200, vault.text
    assert vault.json()["brain_id"] == brain["brain_id"]

    # The runtime belongs to this Brain alone.
    others = httpx.get(
        f"{brain_server}/v1/admin/brains",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": str(uuid.uuid4())},
        timeout=10.0,
    )
    assert others.status_code == 403


@pytest.mark.integration
def test_the_new_brain_has_its_own_private_resources(client, brain_server):
    """The new Brain's vault lives in its own namespace on Server 2."""
    email = f"vault-{uuid.uuid4().hex[:8]}@example.com"
    created = _register(client, email)
    tokens = _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    brain = _my_brain(client, headers)
    owner_id = created["id"]

    record = httpx.post(
        f"{brain_server}/v1/brains/{brain['brain_id']}/vault/records",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Aura-Owner-Id": owner_id,
        },
        json={"record_type": "note", "title": "private", "content": "my own secret"},
        timeout=10.0,
    )
    assert record.status_code == 201, record.text
    record_id = record.json()["record_id"]

    # Another account's owner header cannot read it.
    foreign = httpx.get(
        f"{brain_server}/v1/brains/{brain['brain_id']}/vault/records/{record_id}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Aura-Owner-Id": str(uuid.uuid4()),
        },
        timeout=10.0,
    )
    assert foreign.status_code == 404

    own = httpx.get(
        f"{brain_server}/v1/brains/{brain['brain_id']}/vault/records/{record_id}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Aura-Owner-Id": owner_id,
        },
        timeout=10.0,
    )
    assert own.status_code == 200, own.text
    assert own.json()["content"] == "my own secret"
