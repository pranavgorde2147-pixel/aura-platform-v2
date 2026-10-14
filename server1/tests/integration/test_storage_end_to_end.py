"""End-to-end integration test between Server 1's StorageClient and Server 2's
real object-storage API.

The storage engine runs inside Server 2; ``app.storage_app`` serves the same
``/v1/objects`` and ``/v1/storage/*`` endpoints without loading the brain model.
It is launched as a uvicorn subprocess on a free port with an isolated SQLite
metadata database and local filesystem backend. This exercises the full
authenticated path: internal token validation, owner scoping, upload, list,
metadata, download and delete, plus cross-user isolation.
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

from app.infrastructure.storage_client import StorageClient, StorageServiceError

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER2_DIR = REPO_ROOT / "server2"
TOKEN = "integration-storage-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def storage_server(tmp_path_factory):
    if not (SERVER2_DIR / "app" / "storage_app.py").exists():
        pytest.skip("Server 2 storage app source not available")
    if not hasattr(socket, "AF_INET"):
        pytest.skip("networking unavailable")

    port = _free_port()
    data_dir = tmp_path_factory.mktemp("server2-storage-data")
    env = {
        **os.environ,
        "AURA_SKIP_DOTENV": "1",
        "STORAGE_API_TOKEN": TOKEN,
        "STORAGE_BACKEND": "local",
        "STORAGE_DATA_DIR": str(data_dir),
        "STORAGE_LOCAL_ROOT": str(data_dir / "objects"),
        "STORAGE_DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'storage.db'}",
        "PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.storage_app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(SERVER2_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline and process.poll() is None:
        try:
            if httpx.get(f"{base_url}/v1/storage/health", timeout=2.0).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.3)

    if not ready:
        process.terminate()
        output = process.stdout.read().decode(errors="replace") if process.stdout else ""
        pytest.fail(f"Server 2 storage app failed to start:\n{output}")

    yield base_url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


def _client(base_url: str) -> StorageClient:
    client = StorageClient()
    client.base_url = base_url
    client.token = TOKEN
    return client


@pytest.mark.integration
def test_health_check_reports_ready(storage_server):
    import asyncio

    async def _run():
        result = await _client(storage_server).health_check()
        assert result is not None
        assert result.get("status") == "ready"

    asyncio.run(_run())


@pytest.mark.integration
def test_upload_list_download_delete_roundtrip(storage_server):
    import asyncio

    async def _run():
        client = _client(storage_server)
        owner = str(uuid.uuid4())

        record = await client.upload(
            owner_id=owner,
            filename="notes.txt",
            content=b"hello storage",
            content_type="text/plain",
        )
        assert record["owner_id"] == owner
        object_id = record["id"]

        listing = await client.list_objects(owner_id=owner)
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == object_id

        metadata = await client.get_metadata(owner_id=owner, object_id=object_id)
        assert metadata["filename"] == "notes.txt"

        headers, stream = await client.stream_object(owner_id=owner, object_id=object_id)
        body = b"".join([chunk async for chunk in stream])
        assert body == b"hello storage"
        assert headers["Content-Type"].startswith("text/plain")

        deleted = await client.delete_object(owner_id=owner, object_id=object_id)
        assert deleted is True
        assert await client.get_metadata(owner_id=owner, object_id=object_id) is None

    asyncio.run(_run())


@pytest.mark.integration
def test_cross_user_isolation(storage_server):
    import asyncio

    async def _run():
        client = _client(storage_server)
        owner_a = str(uuid.uuid4())
        owner_b = str(uuid.uuid4())

        record = await client.upload(
            owner_id=owner_a,
            filename="secret.txt",
            content=b"secret-a",
            content_type="text/plain",
        )
        object_id = record["id"]

        assert await client.get_metadata(owner_id=owner_b, object_id=object_id) is None
        assert await client.delete_object(owner_id=owner_b, object_id=object_id) is False
        listing = await client.list_objects(owner_id=owner_b)
        assert listing["total"] == 0

    asyncio.run(_run())


@pytest.mark.integration
def test_invalid_internal_token_is_rejected(storage_server):
    import asyncio

    async def _run():
        client = _client(storage_server)
        client.token = "wrong-token"
        with pytest.raises(StorageServiceError) as exc:
            await client.upload(
                owner_id=str(uuid.uuid4()),
                filename="x.txt",
                content=b"x",
                content_type="text/plain",
            )
        assert exc.value.status_code == 401

    asyncio.run(_run())
