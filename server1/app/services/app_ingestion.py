"""Concrete per-application ingestion adapters.

Each AURA application normalizes its own payload into a
:class:`NormalizedIngestion`, which ``PersonalBrainService.record_ingestion`` then
persists (memory + vault pointer + knowledge nodes + timeline event, with text
indexed for retrieval).

Adapters encode only documented behaviour: the ``sends_to_aura`` fields in
``app/services/app_registry.py`` and the event examples in the implementation
roadmap (Study -> lecture recorded, Meeting -> decisions, Health -> wellness).
No product behaviour is invented; unsupported fields are preserved verbatim in
the timeline metadata rather than dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.models.brain_memory import MemoryKind


class AppIngestionError(Exception):
    """Raised when an application payload cannot be normalized."""


@dataclass(frozen=True)
class NormalizedIngestion:
    """An app-specific payload normalized into a brain ingestion.

    ``kind=None`` (or ``persist_memory=False``) records a timeline/knowledge
    event without creating a brain memory (used for notification-style events).
    """

    content: str
    kind: MemoryKind | None
    event_type: str
    summary: str | None = None
    resource_type: str | None = None
    dedupe_key: str | None = None
    labels: tuple[str, ...] = ()
    importance: int = 10
    vault: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def persist_memory(self) -> bool:
        return self.kind is not None and bool(self.content.strip())


@dataclass(frozen=True)
class AppAdapter:
    """Binds an app slug to the permission it needs and its normalizer."""

    slug: str
    scope: str
    action: str
    normalize: Callable[[dict[str, Any]], NormalizedIngestion]
    description: str


def _require_text(payload: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string among ``keys`` or raise."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (str, dict, list)):
            return str(value)
    raise AppIngestionError(f"Missing required field: one of {', '.join(keys)}")


def _optional_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _string_list(payload: dict[str, Any], *keys: str) -> list[str]:
    """Collect string items from the first present list/dict/str field."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, dict):
            parts = [f"{k}: {v}" for k, v in value.items() if str(v).strip()]
            if parts:
                return parts
    return []


def _first_id(payload: dict[str, Any]) -> str | None:
    for key in ("external_id", "event_id", "document_id", "id", "uuid", "reference"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _vault_from(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a vault pointer dict when the payload references a stored object."""
    reference = (
        payload.get("object_reference")
        or payload.get("object_key")
        or payload.get("file_id")
        or payload.get("document_id")
        or payload.get("attachment")
    )
    if not reference:
        return None
    return {
        "object_reference": str(reference),
        "storage_type": str(payload.get("storage_type") or "object"),
        "mime_type": payload.get("mime_type"),
        "file_size_bytes": payload.get("file_size_bytes"),
        "checksum": payload.get("checksum"),
    }


# --------------------------------------------------------------------- study

def normalize_study(payload: dict[str, Any]) -> NormalizedIngestion:
    """AURA Study: a recorded lecture/study session.

    Documented input (``sends_to_aura``): lecture notes, study records.
    """
    title = _optional_text(payload, "title", "topic", "subject")
    body = _require_text(payload, "notes", "transcript", "content", "text", "summary")
    subject = _optional_text(payload, "subject", "course", "topic")
    content = f"{title}\n{body}" if title and title not in body else body

    labels = ["study", "lecture"]
    if subject:
        labels.append(subject.lower())
    metadata = {
        "title": title,
        "subject": subject,
        "duration_minutes": payload.get("duration_minutes"),
        "course": payload.get("course"),
        "recorded_at": payload.get("recorded_at"),
        "raw": payload,
    }
    return NormalizedIngestion(
        content=content,
        kind=MemoryKind.FACT,
        event_type="study.lecture_recorded",
        summary=title or (body[:120] if body else None),
        resource_type="study_record",
        dedupe_key=_first_id(payload),
        labels=tuple(labels),
        vault=_vault_from(payload),
        metadata=metadata,
    )


# --------------------------------------------------------------------- diary

def normalize_diary(payload: dict[str, Any]) -> NormalizedIngestion:
    """Daily Diary: a personal diary entry (the user's own reflection)."""
    body = _require_text(payload, "entry", "text", "content", "body")
    mood = _optional_text(payload, "mood", "feeling")
    entry_date = _optional_text(payload, "date", "entry_date", "occurred_at")
    metadata = {
        "mood": mood,
        "date": entry_date,
        "raw": payload,
    }
    labels = ["diary"]
    if mood:
        labels.append(f"mood:{mood.lower()}")
    return NormalizedIngestion(
        content=body,
        kind=MemoryKind.INTERPRETATION,
        event_type="diary.entry",
        summary=(body[:120] if body else None),
        resource_type="diary_entry",
        dedupe_key=_first_id(payload),
        labels=tuple(labels),
        metadata=metadata,
    )


# ------------------------------------------------------------------- meeting

def normalize_meeting(payload: dict[str, Any]) -> NormalizedIngestion:
    """AURA Meeting: transcript/summary + decisions + action items.

    Documented input: meeting transcripts, summaries, decisions, action items.
    """
    title = _optional_text(payload, "title", "subject")
    body = _require_text(payload, "transcript", "summary", "notes", "content", "text")
    decisions = _string_list(payload, "decisions")
    action_items = _string_list(payload, "action_items", "actions", "follow_ups")
    participants = _string_list(payload, "participants", "attendees")

    sections = [body]
    if decisions:
        sections.append("Decisions:\n" + "\n".join(f"- {d}" for d in decisions))
    if action_items:
        sections.append("Action items:\n" + "\n".join(f"- {a}" for a in action_items))
    content = "\n\n".join(sections)

    labels = ["meeting"]
    if participants:
        labels.append("meeting:participants")
    metadata = {
        "title": title,
        "decisions": decisions,
        "action_items": action_items,
        "participants": participants,
        "occurred_at": payload.get("occurred_at") or payload.get("date"),
        "raw": payload,
    }
    return NormalizedIngestion(
        content=content,
        kind=MemoryKind.FACT,
        event_type="meeting.recorded",
        summary=title or body[:120],
        resource_type="meeting",
        dedupe_key=_first_id(payload),
        labels=tuple(labels),
        vault=_vault_from(payload),
        metadata=metadata,
    )


# -------------------------------------------------------------------- travel

def normalize_travel(payload: dict[str, Any]) -> NormalizedIngestion:
    """Travel App: a trip/booking record (documented: trip records, itineraries)."""
    destination = _require_text(payload, "destination", "destination_name", "place", "title")
    notes = _optional_text(payload, "notes", "itinerary", "content", "summary")
    start = _optional_text(payload, "start_date", "departure", "from_date")
    end = _optional_text(payload, "end_date", "return_date", "to_date")

    window = " - ".join(part for part in (start, end) if part)
    content_lines = [f"Trip to {destination}"]
    if window:
        content_lines.append(f"Dates: {window}")
    if notes:
        content_lines.append(notes)
    content = "\n".join(content_lines)

    metadata = {
        "destination": destination,
        "start_date": start,
        "end_date": end,
        "raw": payload,
    }
    return NormalizedIngestion(
        content=content,
        kind=MemoryKind.FACT,
        event_type="travel.trip",
        summary=f"Trip to {destination}",
        resource_type="trip",
        dedupe_key=_first_id(payload),
        labels=("travel", destination.lower()),
        vault=_vault_from(payload),
        metadata=metadata,
    )


# -------------------------------------------------------------------- health

def normalize_health(payload: dict[str, Any]) -> NormalizedIngestion:
    """AURA Health: a user-consented wellness event or extracted document.

    The app may extract report text locally and send it in ``extracted_text``.
    The complete text is stored as the vault record content; only explicitly
    selected file metadata is retained alongside it.
    """
    event = _optional_text(payload, "event", "metric", "activity", "type")
    document_text = _optional_text(payload, "extracted_text", "extracted_content")
    body = document_text or _require_text(
        payload, "content", "notes", "summary", "text", "message", "value"
    )
    occurred_at = _optional_text(payload, "occurred_at", "date", "timestamp")
    document_name = _optional_text(payload, "file_name", "document_name", "filename")
    document_type = _optional_text(payload, "file_type", "document_type", "mime_type")
    document_id = _optional_text(payload, "document_id")
    is_document = document_text is not None or document_name is not None or document_id is not None

    content = (
        body
        if is_document or not event or event in body
        else f"{event}: {body}"
    )
    labels = ["health"]
    if event:
        labels.append(f"health:{event.lower()}")
    if is_document:
        labels.append("health:document")
    metadata = {
        "event": event,
        "value": payload.get("value"),
        "unit": payload.get("unit"),
        "occurred_at": occurred_at,
        "document_id": document_id,
        "document_name": document_name,
        "document_type": document_type,
        "extraction_method": _optional_text(payload, "extraction_method"),
        "raw": payload,
    }
    return NormalizedIngestion(
        content=content,
        kind=MemoryKind.FACT,
        event_type="health.medical_report" if is_document else "health.wellness_event",
        summary=document_name or event or (body[:120] if body else None),
        resource_type="health_record" if is_document else "wellness_event",
        dedupe_key=_first_id(payload),
        labels=tuple(labels),
        metadata=metadata,
    )


# -------------------------------------------------------------- notifications

def normalize_notification(payload: dict[str, Any]) -> NormalizedIngestion:
    """Notification Hub: an alert event.

    Notifications are activity events, not personal facts, so they write a
    timeline event and knowledge node but deliberately create no brain memory.
    """
    title = _optional_text(payload, "title", "subject")
    message = _require_text(payload, "message", "body", "text", "content", "title")
    level = _optional_text(payload, "level", "severity") or "info"
    source = _optional_text(payload, "source", "app", "origin")

    metadata = {
        "title": title,
        "level": level,
        "source": source,
        "raw": payload,
    }
    return NormalizedIngestion(
        content=message,
        kind=None,
        event_type="notification.received",
        summary=title or message[:120],
        resource_type="notification",
        dedupe_key=_first_id(payload),
        labels=("notification", level.lower()),
        metadata=metadata,
    )


# --------------------------------------------------------------------- chat

def normalize_chat(payload: dict[str, Any]) -> NormalizedIngestion:
    """AURA Chat: a conversation turn the user opted to persist.

    AURA Chat's primary surface is ``POST /api/v1/brain/query``; this adapter
    covers explicitly persisted chat messages. The message is the user's own
    text, stored as an interpretation to avoid treating it as ground truth.
    """
    message = _require_text(payload, "message", "content", "text", "role_content")
    role = _optional_text(payload, "role") or "user"
    metadata = {
        "role": role,
        "conversation_id": payload.get("conversation_id"),
        "raw": payload,
    }
    return NormalizedIngestion(
        content=message,
        kind=MemoryKind.INTERPRETATION,
        event_type="chat.message",
        summary=message[:120],
        resource_type="chat_message",
        dedupe_key=_first_id(payload),
        labels=("chat", role.lower()),
        metadata=metadata,
    )


def _metadata_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, default=str)


ADAPTERS: dict[str, AppAdapter] = {
    "brain": AppAdapter(
        slug="brain",
        scope="memory",
        action="write",
        normalize=normalize_chat,
        description="AURA Chat (persisted conversation turns).",
    ),
    "study": AppAdapter(
        slug="study",
        scope="memory",
        action="write",
        normalize=normalize_study,
        description="Study Companion lecture/study records.",
    ),
    "diary": AppAdapter(
        slug="diary",
        scope="memory",
        action="write",
        normalize=normalize_diary,
        description="Daily Diary entries.",
    ),
    "meeting": AppAdapter(
        slug="meeting",
        scope="memory",
        action="write",
        normalize=normalize_meeting,
        description="Meeting Assistant transcripts/decisions/action items.",
    ),
    "travel": AppAdapter(
        slug="travel",
        scope="memory",
        action="write",
        normalize=normalize_travel,
        description="Travel App trip records.",
    ),
    "health": AppAdapter(
        slug="health",
        scope="memory",
        action="write",
        normalize=normalize_health,
        description="Health/Wellness consented wellness events.",
    ),
    "notifications": AppAdapter(
        slug="notifications",
        scope="notifications",
        action="write",
        normalize=normalize_notification,
        description="Notification Hub alert events.",
    ),
}


def get_adapter(slug: str) -> AppAdapter | None:
    """Return the ingestion adapter for an app slug, or None."""
    return ADAPTERS.get(slug)


def list_ingestion_apps() -> list[str]:
    """Slugs that currently have a concrete ingestion integration."""
    return sorted(ADAPTERS)
