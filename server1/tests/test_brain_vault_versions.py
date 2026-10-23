from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import vault as vault_api


@pytest.mark.parametrize(
    ("record_namespace", "expected_status", "expected_calls"),
    [
        ("apps/diary", None, 2),
        ("apps/health", 404, 1),
    ],
)
def test_list_record_versions_checks_record_namespace(
    monkeypatch, record_namespace, expected_status, expected_calls
):
    user_id = uuid.uuid4()
    brain_id = uuid.uuid4()
    record_id = uuid.uuid4()
    calls = []

    async def authorize(*args, **kwargs):
        assert kwargs["source_app"] == "diary"
        assert kwargs["action"] == "read"

    async def owned_brain(*args, **kwargs):
        return None, SimpleNamespace(id=brain_id)

    async def call_server2(requested_user_id, requested_brain_id, **kwargs):
        assert requested_user_id == user_id
        assert requested_brain_id == brain_id
        calls.append(kwargs)
        if kwargs["path"] == f"records/{record_id}":
            return {
                "record_id": str(record_id),
                "source_app": "diary",
                "namespace": record_namespace,
            }
        return {"versions": []}

    monkeypatch.setattr(vault_api, "_authorize_record_action", authorize)
    monkeypatch.setattr(vault_api, "_owned_brain", owned_brain)
    monkeypatch.setattr(vault_api, "_call_server2", call_server2)

    async def invoke():
        return await vault_api.list_record_versions(
            record_id=record_id,
            source_app="diary",
            current_user=SimpleNamespace(id=user_id),
            session=object(),
        )

    if expected_status is None:
        assert asyncio.run(invoke()) == {"versions": []}
    else:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(invoke())
        assert exc_info.value.status_code == expected_status

    assert len(calls) == expected_calls
    assert calls[0]["path"] == f"records/{record_id}"
