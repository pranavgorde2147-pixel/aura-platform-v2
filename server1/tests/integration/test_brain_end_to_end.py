"""End-to-end integration test between Server 1's BrainClient and Server 2's
real per-user Brain platform.

Server 2's ``app.main`` serves the Brain registry (``/v1/brains/*``), the
per-Brain vault, apps and jobs, and the inference endpoints (``/v1/brain/*``).
It is launched as a uvicorn subprocess on a free port with an isolated SQLite
metadata database, an isolated resource root and no model artifact, so the
Brain runs on the deterministic simulator and reports itself honestly as
``simulated``.

This exercises the full authenticated path: internal token validation, the
canonical Brain identity, provisioning of a dedicated runtime per Brain,
inference on that Brain's own runtime, owner isolation and the administrative
registry API.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from app.infrastructure.brain_client import BrainClient, BrainServiceError

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER2_DIR = REPO_ROOT / "server2"
TOKEN = "integration-brain-token"
ADMIN_OWNER = str(uuid.uuid4())


def _server2_python() -> str:
    """Return the interpreter that can import Server 2's brain service.

    ``app.main`` imports torch and transformers at module level, so the brain
    service needs Server 2's own virtualenv. The storage-only app does not,
    which is why the storage integration test can use Server 1's venv.
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
    data_dir = tmp_path_factory.mktemp("server2-brain-data")
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
        "BRAIN_ADMIN_OWNER_IDS": ADMIN_OWNER,
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


def _client(base_url: str) -> BrainClient:
    client = BrainClient()
    client.base_url = base_url
    client.token = TOKEN
    client.model = "Qwen3.5-0.8B"
    return client


@pytest.mark.integration
def test_provisioning_is_idempotent_and_grants_a_dedicated_runtime(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        owner = str(uuid.uuid4())
        brain_id = str(uuid.uuid4())
        identity_id = str(uuid.uuid4())

        first = await client.provision_brain(
            brain_id=brain_id,
            owner_id=owner,
            ai_identity_id=identity_id,
            model_id="Qwen3.5-0.8B",
        )
        second = await client.provision_brain(
            brain_id=brain_id,
            owner_id=owner,
            ai_identity_id=identity_id,
            model_id="Qwen3.5-0.8B",
        )

        assert first["brain_id"] == second["brain_id"] == brain_id
        assert first["owner_id"] == owner
        assert first["ai_identity_id"] == identity_id
        assert first["provisioning_status"] == "ready"
        # The Brain owns exactly one runtime, and the runtime is reported
        # honestly (the CI deployment has no model artifact).
        assert first["llm_runtime_id"]
        assert first["llm_runtime_id"] == second["llm_runtime_id"]
        assert first["runtime_isolation"] == "simulated"
        assert first["llm_runtime_status"] == "ready_simulated"
        # Every private resource is Brain-scoped.
        assert first["memory_resource_reference"].startswith(f"brain_{brain_id}")
        assert first["vector_resource_reference"].startswith(f"brain_{brain_id}")
        assert first["vault_resource_reference"].startswith(f"brain_{brain_id}")
        assert first["storage_resource_reference"].endswith(brain_id)

    asyncio.run(_run())


@pytest.mark.integration
def test_each_owner_gets_its_own_runtime(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        first = await client.provision_brain(
            brain_id=str(uuid.uuid4()),
            owner_id=str(uuid.uuid4()),
            ai_identity_id=str(uuid.uuid4()),
            model_id="Qwen3.5-0.8B",
        )
        second = await client.provision_brain(
            brain_id=str(uuid.uuid4()),
            owner_id=str(uuid.uuid4()),
            ai_identity_id=str(uuid.uuid4()),
            model_id="Qwen3.5-0.8B",
        )
        assert first["llm_runtime_id"] != second["llm_runtime_id"]
        assert first["resource_root"] != second["resource_root"]

    asyncio.run(_run())


@pytest.mark.integration
def test_inference_runs_on_the_brains_own_runtime(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        owner = str(uuid.uuid4())
        brain_id = str(uuid.uuid4())
        provisioned = await client.provision_brain(
            brain_id=brain_id,
            owner_id=owner,
            ai_identity_id=str(uuid.uuid4()),
            model_id="Qwen3.5-0.8B",
        )

        answer = await client.infer(
            user_id=owner,
            query="Give a short answer.",
            context={"user_level": "general", "relevant_context": {}, "safety_constraints": []},
        )
        assert answer["brain_id"] == brain_id
        assert answer["llm_runtime_id"] == provisioned["llm_runtime_id"]
        assert answer["status"] == "ok"
        assert isinstance(answer["response"], str)

    asyncio.run(_run())


@pytest.mark.integration
def test_inference_requires_a_provisioned_brain(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        with pytest.raises(BrainServiceError) as exc:
            await client.infer(
                user_id=str(uuid.uuid4()),
                query="Give a short answer.",
                context={"user_level": "general", "relevant_context": {}, "safety_constraints": []},
            )
        assert exc.value.status_code == 503

    asyncio.run(_run())


@pytest.mark.integration
def test_vault_is_owner_isolated_end_to_end(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        owner_a = str(uuid.uuid4())
        owner_b = str(uuid.uuid4())
        brain_a = str(uuid.uuid4())
        await client.provision_brain(
            brain_id=brain_a,
            owner_id=owner_a,
            ai_identity_id=str(uuid.uuid4()),
            model_id="Qwen3.5-0.8B",
        )

        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "X-Aura-Owner-Id": owner_a,
        }
        created = httpx.post(
            f"{brain_server}/v1/brains/{brain_a}/vault/records",
            headers=headers,
            json={"record_type": "note", "title": "private", "content": "owner a secret"},
            timeout=10.0,
        )
        assert created.status_code == 201, created.text
        record_id = created.json()["record_id"]

        # Another owner cannot see the record.
        foreign = httpx.get(
            f"{brain_server}/v1/brains/{brain_a}/vault/records/{record_id}",
            headers={**headers, "X-Aura-Owner-Id": owner_b},
            timeout=10.0,
        )
        assert foreign.status_code == 404

        # The owner can.
        own = httpx.get(
            f"{brain_server}/v1/brains/{brain_a}/vault/records/{record_id}",
            headers=headers,
            timeout=10.0,
        )
        assert own.status_code == 200, own.text
        assert own.json()["content"] == "owner a secret"

    asyncio.run(_run())


@pytest.mark.integration
def test_admin_api_requires_administrative_authorization(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        owner = str(uuid.uuid4())
        await client.provision_brain(
            brain_id=str(uuid.uuid4()),
            owner_id=owner,
            ai_identity_id=str(uuid.uuid4()),
            model_id="Qwen3.5-0.8B",
        )

        # A normal owner is refused.
        refused = httpx.get(
            f"{brain_server}/v1/admin/brains",
            headers={"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": owner},
            timeout=10.0,
        )
        assert refused.status_code == 403

        # The allowlisted administrator is allowed.
        allowed = httpx.get(
            f"{brain_server}/v1/admin/brains",
            headers={"Authorization": f"Bearer {TOKEN}", "X-Aura-Owner-Id": ADMIN_OWNER},
            timeout=10.0,
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["total"] >= 1

    asyncio.run(_run())


@pytest.mark.integration
def test_invalid_internal_token_is_rejected(brain_server):
    import asyncio

    async def _run():
        client = _client(brain_server)
        client.token = "wrong-token"
        with pytest.raises(BrainServiceError) as exc:
            await client.provision_brain(
                brain_id=str(uuid.uuid4()),
                owner_id=str(uuid.uuid4()),
                ai_identity_id=str(uuid.uuid4()),
                model_id="Qwen3.5-0.8B",
            )
        assert exc.value.status_code == 401

    asyncio.run(_run())
