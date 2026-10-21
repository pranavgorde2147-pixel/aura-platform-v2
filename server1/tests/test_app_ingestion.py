"""Tests for concrete application ingestion adapters and the ingestion pipeline."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.brain_memory import MemoryKind, MemorySource
from app.services.app_ingestion import (
    ADAPTERS,
    AppIngestionError,
    get_adapter,
    list_ingestion_apps,
    normalize_chat,
    normalize_diary,
    normalize_meeting,
    normalize_notification,
    normalize_study,
    normalize_travel,
)
from app.services.brain_provider import MockPersonalBrainLLMProvider
from app.services.personal_brain import (
    AppIngestionPersistenceError,
    PersonalBrainService,
)


# ----------------------------------------------------------------- adapters

def test_study_adapter_extracts_document_and_dedupe_key():
    normalized = normalize_study(
        {
            "title": "DBMS Normalization",
            "notes": "1NF, 2NF, 3NF details",
            "subject": "Databases",
            "object_reference": "objects/lecture-1.pdf",
            "external_id": "lecture-1",
        }
    )
    assert normalized.kind == MemoryKind.FACT
    assert normalized.event_type == "study.lecture_recorded"
    assert "DBMS Normalization" in normalized.content
    assert "1NF" in normalized.content
    assert normalized.dedupe_key == "lecture-1"
    assert normalized.vault["object_reference"] == "objects/lecture-1.pdf"
    assert "study" in normalized.labels


def test_diary_adapter_is_interpretation():
    normalized = normalize_diary({"entry": "Felt productive today", "mood": "good"})
    assert normalized.kind == MemoryKind.INTERPRETATION
    assert normalized.event_type == "diary.entry"
    assert normalized.content == "Felt productive today"
    assert "mood:good" in normalized.labels


def test_meeting_adapter_includes_decisions_and_actions():
    normalized = normalize_meeting(
        {
            "title": "Roadmap sync",
            "transcript": "We discussed Q3 priorities.",
            "decisions": ["Ship auth first"],
            "action_items": ["Write tests"],
        }
    )
    assert normalized.event_type == "meeting.recorded"
    assert "Decisions:" in normalized.content
    assert "Ship auth first" in normalized.content
    assert "Write tests" in normalized.content
    assert normalized.metadata["decisions"] == ["Ship auth first"]


def test_meeting_adapter_requires_transcript_like_field():
    with pytest.raises(AppIngestionError):
        normalize_meeting({"decisions": ["only decisions"]})


def test_travel_adapter_requires_destination():
    with pytest.raises(AppIngestionError):
        normalize_travel({"notes": "somewhere"})
    normalized = normalize_travel({"destination": "Kyoto", "start_date": "2026-04-01"})
    assert normalized.event_type == "travel.trip"
    assert "Kyoto" in normalized.content


def test_notification_adapter_writes_no_memory():
    normalized = normalize_notification({"message": "Meeting starting", "level": "warning"})
    assert normalized.kind is None
    assert normalized.persist_memory is False
    assert normalized.event_type == "notification.received"


def test_chat_adapter_is_interpretation():
    normalized = normalize_chat({"message": "Plan my week", "role": "user"})
    assert normalized.kind == MemoryKind.INTERPRETATION
    assert normalized.event_type == "chat.message"


def test_adapter_registry_shape():
    assert get_adapter("does-not-exist") is None
    assert "study" in list_ingestion_apps()
    assert ADAPTERS["notifications"].scope == "notifications"
    assert ADAPTERS["brain"].action == "write"


# -------------------------------------------------------- ingestion pipeline

def make_user(email: str = "ingest@example.com") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email=email)


def build_service(identity) -> PersonalBrainService:
    session = SimpleNamespace(add=Mock(), flush=AsyncMock())
    service = PersonalBrainService(
        session, llm_provider=MockPersonalBrainLLMProvider()
    )
    service.identity_repo = SimpleNamespace(
        get_by_user_id=AsyncMock(return_value=identity)
    )
    service.brain_memory_repo = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        commit=AsyncMock(),
    )
    service.app_record_repo = SimpleNamespace(
        find_by_dedupe=AsyncMock(return_value=None),
    )
    service.memory_repo = SimpleNamespace(
        upsert_for_identity=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    service.knowledge_repo = SimpleNamespace(
        record_nodes=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    service.timeline_repo = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        find_by_reference=AsyncMock(return_value=None),
    )
    return service


def test_record_ingestion_persists_memory_vault_and_timeline():
    async def _run():
        user = make_user()
        service = build_service(SimpleNamespace(id=uuid.uuid4()))
        adapter = get_adapter("study")
        result = await service.ingest_app_event(
            user,
            adapter,
            {"notes": "Lecture notes", "object_reference": "objects/l.pdf", "external_id": "l1"},
        )

        assert result["accepted"] is True
        assert result["duplicate"] is False
        assert result["partial"] is False
        assert result["memory_id"] is not None
        assert result["vault_entry_id"] is not None

        create_kwargs = service.brain_memory_repo.create.await_args.kwargs
        assert create_kwargs["source"] == MemorySource.APP
        assert create_kwargs["source_app"] == "study"
        service.memory_repo.upsert_for_identity.assert_awaited_once()
        service.knowledge_repo.record_nodes.assert_awaited_once()
        service.timeline_repo.create.assert_awaited_once()
        service.timeline_repo.commit.assert_awaited_once()

    asyncio.run(_run())


def test_record_ingestion_keeps_full_content_out_of_brain_memory():
    """The full content belongs to application storage, memory keeps a summary."""
    async def _run():
        user = make_user()
        service = build_service(SimpleNamespace(id=uuid.uuid4()))
        adapter = get_adapter("study")
        result = await service.ingest_app_event(
            user,
            adapter,
            {
                "title": "DBMS Normalization",
                "notes": "1NF, 2NF, 3NF details",
                "subject": "Databases",
                "external_id": "lecture-1",
            },
        )

        assert result["accepted"] is True
        assert result["record_id"]

        stored_record = service.session.add.call_args.args[0]
        assert stored_record.destination_app == "study"
        assert "1NF, 2NF, 3NF details" in stored_record.content

        memory_kwargs = service.brain_memory_repo.create.await_args.kwargs
        assert memory_kwargs["content"] != stored_record.content
        assert len(memory_kwargs["content"]) <= 1000

    asyncio.run(_run())


def test_record_ingestion_dedupes_on_external_id():
    async def _run():
        user = make_user()
        service = build_service(SimpleNamespace(id=uuid.uuid4()))
        existing = SimpleNamespace(event_metadata=json.dumps({"memory_id": str(uuid.uuid4())}))
        service.timeline_repo.find_by_reference = AsyncMock(return_value=existing)

        result = await service.ingest_app_event(
            user,
            get_adapter("study"),
            {"notes": "repeat", "external_id": "same-id"},
        )

        assert result["duplicate"] is True
        assert result["memory_id"] == json.loads(existing.event_metadata)["memory_id"]
        service.brain_memory_repo.create.assert_not_awaited()
        service.timeline_repo.create.assert_not_awaited()

    asyncio.run(_run())


def test_record_ingestion_partial_failure_preserves_memory():
    async def _run():
        user = make_user()
        service = build_service(SimpleNamespace(id=uuid.uuid4()))
        service.knowledge_repo.record_nodes = AsyncMock(side_effect=RuntimeError("graph down"))

        result = await service.ingest_app_event(
            user, get_adapter("diary"), {"entry": "A difficult day"}
        )

        assert result["accepted"] is True
        assert result["partial"] is True
        assert result["memory_id"] is not None
        service.timeline_repo.rollback.assert_awaited()

    asyncio.run(_run())


def test_record_ingestion_fails_when_canonical_record_cannot_be_persisted():
    async def _run():
        user = make_user()
        service = build_service(SimpleNamespace(id=uuid.uuid4()))
        service._store_application_record = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with pytest.raises(AppIngestionPersistenceError):
            await service.ingest_app_event(
                user,
                get_adapter("diary"),
                {"entry": "My pet name is Sai"},
            )

        service.brain_memory_repo.create.assert_not_awaited()
        service.timeline_repo.create.assert_not_awaited()

    asyncio.run(_run())


def test_record_ingestion_without_identity_is_rejected():
    async def _run():
        user = make_user()
        service = build_service(None)
        result = await service.ingest_app_event(
            user, get_adapter("travel"), {"destination": "Paris"}
        )
        assert result["accepted"] is False
        assert result["reason"] == "brain_not_found"

    asyncio.run(_run())
