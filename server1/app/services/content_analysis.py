"""Server 1 side of the understanding layer.

Server 2 owns the understanding implementation. This module is the Server 1
client for it, plus an explicit, honest fallback used when Server 2 cannot be
reached.

The fallback never guesses a category. It reports the input as a single
``unclassified`` segment with a confidence of ``0.0``, which the routing policy
always sends to review. That keeps the platform honest: without the
understanding service, AURA does not pretend to understand anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.infrastructure.brain_client import BrainServiceError, get_brain_client
from app.services.classification_policy import ClassificationPolicy

logger = logging.getLogger(__name__)

VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "study_material",
        "lecture",
        "meeting",
        "task",
        "deadline",
        "calendar_event",
        "diary_event",
        "health",
        "travel",
        "note",
        "document",
        "personal_fact",
        "user_preference",
        "project",
        "unclassified",
    }
)

# The record-type vocabulary is shared with Server 2 (``server2/app/analysis.py``).
# Keeping the two lists identical matters: a record type that Server 2 produced
# and Server 1 silently rewrote to "note" would lose the distinction between a
# lecture transcript and a lecture note, and with it the correct destination.
VALID_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "lecture_note",
        "lecture_transcript",
        "study_material",
        "concept",
        "meeting_transcript",
        "meeting_summary",
        "decision",
        "action_item",
        "task",
        "assignment_deadline",
        "calendar_event",
        "diary_entry",
        "personal_event",
        "wellness_event",
        "health_record",
        "trip",
        "itinerary",
        "travel_booking",
        "note",
        "document",
        "memory_fact",
        "memory_preference",
        "project_note",
    }
)


@dataclass
class AnalysisSegment:
    """One independently meaningful piece of the submitted input."""

    index: int
    category: str
    record_type: str
    text: str
    summary: str
    confidence: float
    rationale: str = ""
    entities: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None


@dataclass
class AnalysisResult:
    """The full understanding result for one submitted input."""

    segments: list[AnalysisSegment]
    analysis_mode: str
    confidence_kind: str
    provider: str
    degraded: bool = False
    degraded_reason: str | None = None
    request_id: str | None = None

    @property
    def is_heuristic(self) -> bool:
        """True when the confidence values are not calibrated."""
        return self.confidence_kind == "heuristic"


class ContentAnalysisService:
    """Produces a structured understanding of one submitted input."""

    def __init__(
        self,
        policy: ClassificationPolicy | None = None,
        client=None,
    ) -> None:
        self.policy = policy or ClassificationPolicy.from_settings()
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = get_brain_client()
        return self._client

    async def analyze(
        self,
        *,
        user_id: str,
        content: str,
        source_app: str | None = None,
        language: str | None = None,
        context: dict | None = None,
        request_id: str | None = None,
        max_segments: int | None = None,
    ) -> AnalysisResult:
        """Return the structured understanding of ``content``.

        Never raises for a service failure: an unreachable understanding
        service produces a degraded, unclassified result so the caller can
        report the run as needing review instead of failing the request.
        """
        limit = max_segments or self.policy.max_segments
        try:
            payload = await self.client.analyze(
                user_id=user_id,
                content=content,
                context=context or {},
                source_app=source_app,
                language=language,
                request_id=request_id,
                max_segments=limit,
            )
        except BrainServiceError as exc:
            logger.warning(
                "Understanding service unavailable (%s); falling back to an unclassified result",
                exc.error_code,
            )
            return self._fallback(content, reason=f"analysis_unavailable:{exc.error_code}")

        segments = self._parse_segments(payload.get("segments"))
        if not segments:
            return self._fallback(content, reason="analysis_empty")

        analysis_mode = str(payload.get("analysis_mode") or "deterministic")
        confidence_kind = str(payload.get("confidence_kind") or "heuristic")
        degraded = bool(payload.get("degraded"))
        return AnalysisResult(
            segments=segments,
            analysis_mode=analysis_mode,
            confidence_kind=confidence_kind,
            provider=str(payload.get("provider") or "unknown"),
            degraded=degraded,
            degraded_reason=payload.get("degraded_reason"),
            request_id=payload.get("request_id"),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_segments(self, raw: object) -> list[AnalysisSegment]:
        """Normalise the segments returned by the understanding service."""
        if not isinstance(raw, list):
            return []
        segments: list[AnalysisSegment] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            category = item.get("category")
            category = category if category in VALID_CATEGORIES else "unclassified"
            record_type = item.get("record_type")
            record_type = record_type if record_type in VALID_RECORD_TYPES else "note"
            entities = item.get("entities")
            entities = [str(entity)[:200] for entity in entities] if isinstance(entities, list) else []
            segments.append(
                AnalysisSegment(
                    index=int(item.get("index", index)) if isinstance(item.get("index", index), int) else index,
                    category=category,
                    record_type=record_type,
                    text=text.strip(),
                    summary=str(item.get("summary") or text.strip())[:500],
                    confidence=self._coerce_confidence(item.get("confidence")),
                    rationale=str(item.get("rationale") or "")[:300],
                    entities=entities,
                    occurred_at=self._parse_datetime(item.get("occurred_at")),
                )
            )
        return segments

    @staticmethod
    def _coerce_confidence(value: object) -> float:
        try:
            confidence = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _fallback(self, content: str, *, reason: str) -> AnalysisResult:
        """Return an explicitly unclassified, review-required result."""
        text = content.strip()
        return AnalysisResult(
            segments=[
                AnalysisSegment(
                    index=0,
                    category="unclassified",
                    record_type="note",
                    text=text,
                    summary=text[:200],
                    confidence=0.0,
                    rationale="The understanding service was unavailable.",
                )
            ],
            analysis_mode="deterministic",
            confidence_kind="heuristic",
            provider="local_fallback",
            degraded=True,
            degraded_reason=reason,
        )
