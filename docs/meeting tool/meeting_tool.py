#!/usr/bin/env python3
"""A lightweight meeting-ingestion helper for AURA-style meeting data.

This tool mirrors the repository's normalize_meeting behavior and makes it easy
for local scripts or adapters to turn a raw meeting payload into a normalized
AURA record.
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def _optional_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _require_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    raise ValueError(f"Missing required field: one of {', '.join(keys)}")


def _string_list(payload: dict[str, Any], *keys: str) -> list[str]:
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


def normalize_meeting(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw meeting payload into an AURA-ready record."""
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

    normalized = {
        "event_type": "meeting.recorded",
        "kind": "fact",
        "resource_type": "meeting",
        "summary": title or body[:120],
        "title": title,
        "content": "\n\n".join(sections),
        "labels": ["meeting"],
        "decisions": decisions,
        "action_items": action_items,
        "participants": participants,
        "occurred_at": payload.get("occurred_at") or payload.get("date"),
        "metadata": {
            "title": title,
            "decisions": decisions,
            "action_items": action_items,
            "participants": participants,
            "occurred_at": payload.get("occurred_at") or payload.get("date"),
            "raw": payload,
        },
    }
    if participants:
        normalized["labels"].append("meeting:participants")
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize AURA meeting payloads.")
    parser.add_argument("payload", nargs="?", help="JSON meeting payload")
    parser.add_argument("--file", type=str, help="Path to a JSON file containing the meeting payload")
    args = parser.parse_args()

    payload_source = args.payload
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            payload_source = handle.read()

    if payload_source is None:
        payload_source = "{}"

    try:
        payload = json.loads(payload_source)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Meeting payload must be a JSON object.")

    print(json.dumps(normalize_meeting(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
