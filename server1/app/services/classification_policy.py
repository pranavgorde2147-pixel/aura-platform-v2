"""Classification policy for the Brain processing pipeline.

This module holds the *policy* side of understanding: how risky a category is,
which confidence threshold applies, when memory may be written and when an
item must be reviewed by the user instead of being delivered.

Confidence values produced by the understanding layer are **not calibrated**.
They are used as a relative signal between a policy threshold and a category,
and every response says whether a score came from the model or from the
deterministic (heuristic) analyser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from app.core.config import Settings, get_settings


class RiskTier(str, Enum):
    """How consequential an automatic delivery is."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Category -> risk tier. The tier selects the confidence threshold and
#: decides whether the operation additionally requires explicit confirmation.
CATEGORY_RISK: dict[str, RiskTier] = {
    "note": RiskTier.LOW,
    "document": RiskTier.LOW,
    "project": RiskTier.LOW,
    "study_material": RiskTier.MEDIUM,
    "lecture": RiskTier.MEDIUM,
    "meeting": RiskTier.MEDIUM,
    "task": RiskTier.MEDIUM,
    "deadline": RiskTier.MEDIUM,
    "travel": RiskTier.MEDIUM,
    "diary_event": RiskTier.MEDIUM,
    "health": RiskTier.HIGH,
    "calendar_event": RiskTier.HIGH,
    "personal_fact": RiskTier.HIGH,
    "user_preference": RiskTier.HIGH,
    "unclassified": RiskTier.HIGH,
}

#: Categories whose records must never be written to memory automatically.
MEMORY_EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {"health", "diary_event", "meeting", "lecture", "study_material", "travel", "document"}
)

#: Categories that are meaningful to remember for future reasoning.
MEMORY_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"personal_fact", "user_preference"})


class SensitiveContentDetector:
    """Detects content that must never become conversational memory."""

    _PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bpassword\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
        re.compile(r"\bpasswd\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
        re.compile(r"\bapi[\s_-]?key\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
        re.compile(r"\bsecret\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
        re.compile(r"\baccess[\s_-]?token\s*(?:is|:|=)\s*\S+", re.IGNORECASE),
        re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"\b(?:cvv|cvc|card\s*number)\s*(?:is|:|=)?\s*\d{3,4}\b", re.IGNORECASE),
        re.compile(r"\b\d{13,16}\b(?=[^0-9]*$)"),
    )

    def contains_secret(self, text: str | None) -> bool:
        """Return True when the text looks like it carries a credential."""
        if not text:
            return False
        return any(pattern.search(text) for pattern in self._PATTERNS)


@dataclass(frozen=True)
class ClassificationPolicy:
    """Resolved, immutable policy used by the routing pipeline."""

    note_threshold: float
    medium_threshold: float
    sensitive_threshold: float
    auto_deliver: bool
    memory_write_enabled: bool
    memory_summary_max_chars: int
    memory_retention_days: int
    record_retention_days: int
    max_delivery_retries: int
    max_segments: int
    idempotency_required: bool
    retain_source_content: bool

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ClassificationPolicy":
        settings = settings or get_settings()
        return cls(
            note_threshold=settings.brain_routing_confidence_note,
            medium_threshold=settings.brain_routing_confidence_medium,
            sensitive_threshold=settings.brain_routing_confidence_sensitive,
            auto_deliver=settings.brain_routing_auto_deliver,
            memory_write_enabled=settings.brain_routing_memory_write_enabled,
            memory_summary_max_chars=settings.brain_routing_memory_summary_max_chars,
            memory_retention_days=settings.brain_routing_memory_retention_days,
            record_retention_days=settings.brain_routing_record_retention_days,
            max_delivery_retries=settings.brain_routing_max_delivery_retries,
            max_segments=settings.brain_routing_max_segments,
            idempotency_required=settings.brain_routing_idempotency_required,
            retain_source_content=True,
        )

    def risk_tier(self, category: str) -> RiskTier:
        return CATEGORY_RISK.get(category, RiskTier.HIGH)

    def threshold_for(self, category: str) -> float:
        """Return the confidence threshold that applies to a category."""
        tier = self.risk_tier(category)
        if tier is RiskTier.LOW:
            return self.note_threshold
        if tier is RiskTier.MEDIUM:
            return self.medium_threshold
        return self.sensitive_threshold

    def accepts(self, category: str, confidence: float, *, explicit: bool = False) -> bool:
        """Return True when an item may be delivered without user review.

        ``explicit`` marks an item the user directly asked for; an explicit
        instruction lowers the bar, but an unclassified item is still never
        delivered automatically.
        """
        if category == "unclassified":
            return False
        if explicit:
            return True
        if not self.auto_deliver:
            return False
        return confidence >= self.threshold_for(category)

    def requires_confirmation(self, category: str) -> bool:
        """High-risk operations always need an explicit user confirmation."""
        return self.risk_tier(category) is RiskTier.HIGH

    def memory_allowed(self, category: str) -> bool:
        """Return True when a category may contribute to Brain memory."""
        if not self.memory_write_enabled:
            return False
        if category in MEMORY_EXCLUDED_CATEGORIES:
            return False
        return category in MEMORY_ELIGIBLE_CATEGORIES
