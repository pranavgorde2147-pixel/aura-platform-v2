"""Qdrant health probe: safe failure categories, no credential leakage."""

from __future__ import annotations

import httpx
from qdrant_client.http.exceptions import UnexpectedResponse

from app.infrastructure.qdrant.health import check_qdrant_health, probe_qdrant


class FakeQdrant:
    def __init__(self, *, raises: Exception | None = None, result=None) -> None:
        self._raises = raises
        self._result = result if result is not None else []

    def get_collections(self):
        if self._raises is not None:
            raise self._raises
        return self._result


def _unexpected(status_code: int) -> UnexpectedResponse:
    return UnexpectedResponse(
        status_code=status_code,
        reason_phrase="Forbidden",
        content=b"super-secret-token-abc",
        headers=httpx.Headers(),
    )


def test_successful_probe():
    assert probe_qdrant(FakeQdrant()).ok is True
    assert check_qdrant_health(FakeQdrant()) is True


def test_forbidden_is_classified_without_leaking_body():
    probe = probe_qdrant(FakeQdrant(raises=_unexpected(403)))
    assert probe.ok is False
    assert probe.reason == "forbidden"
    assert "super-secret-token-abc" not in (probe.reason or "")


def test_unauthorized_is_classified():
    probe = probe_qdrant(FakeQdrant(raises=_unexpected(401)))
    assert probe.reason == "unauthorized"


def test_other_http_status_is_classified():
    probe = probe_qdrant(FakeQdrant(raises=_unexpected(500)))
    assert probe.reason == "http_500"


def test_connection_error_is_classified():
    probe = probe_qdrant(FakeQdrant(raises=ConnectionError("boom")))  # noqa: F821
    assert probe.reason == "connection_error"


def test_timeout_is_classified():
    class ReadTimeout(Exception):
        pass

    probe = probe_qdrant(FakeQdrant(raises=ReadTimeout()))
    assert probe.reason == "timeout"


def test_check_qdrant_health_returns_false_on_failure():
    assert check_qdrant_health(FakeQdrant(raises=_unexpected(403))) is False
