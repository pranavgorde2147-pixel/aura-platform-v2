"""Dedicated local tools for saving, finding, and reviewing meeting records."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from meeting_tool import normalize_meeting


DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "aura-meeting" / "meetings.sqlite3"
STOP_WORDS = {
    "about", "after", "again", "from", "have", "into", "meeting", "please",
    "that", "them", "there", "this", "what", "when", "where", "which", "with",
    "would", "could", "should", "were", "will", "your",
}


def _database_path(db_path: str | Path | None = None) -> Path:
    path = Path(db_path or os.environ.get("AURA_MEETING_DB", DEFAULT_DB_PATH)).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            occurred_at TEXT,
            participants TEXT NOT NULL,
            content TEXT NOT NULL,
            record_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return connection


@contextmanager
def _connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = _connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "occurred_at": row["occurred_at"],
        "participants": json.loads(row["participants"]),
        "content": row["content"],
        "created_at": row["created_at"],
        "record": json.loads(row["record_json"]),
    }


def save_meeting(
    payload: dict[str, Any], *, db_path: str | Path | None = None
) -> dict[str, Any]:
    """Normalize and persist one meeting record in the local meeting database."""
    record = normalize_meeting(payload)
    meeting_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO meetings
                (id, title, occurred_at, participants, content, record_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                record["title"] or record["summary"],
                record["occurred_at"],
                json.dumps(record["participants"], ensure_ascii=False),
                record["content"],
                json.dumps(record, ensure_ascii=False),
                created_at,
            ),
        )
    return {
        "id": meeting_id,
        "title": record["title"] or record["summary"],
        "occurred_at": record["occurred_at"],
        "created_at": created_at,
        "record": record,
    }


def list_meetings(
    *, limit: int = 50, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """List recent meetings, newest first."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_decode_row(row) for row in rows]


def get_meeting(meeting_id: str, *, db_path: str | Path | None = None) -> dict[str, Any]:
    """Get one saved meeting or raise ``LookupError`` if it does not exist."""
    with _connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    if row is None:
        raise LookupError(f"Meeting not found: {meeting_id}")
    return _decode_row(row)


def search_meetings(
    query: str, *, limit: int = 20, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Find saved records containing all query terms, case-insensitively."""
    terms = [
        term
        for term in re.findall(r"\w+", query.casefold())
        if term not in STOP_WORDS
    ]
    if not terms:
        terms = re.findall(r"\w+", query.casefold())
    if not terms:
        raise ValueError("Search query must contain at least one word")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    conditions = " AND ".join(
        "(lower(title) LIKE ? OR lower(content) LIKE ? OR lower(participants) LIKE ?)"
        for _ in terms
    )
    parameters = [pattern for term in terms for pattern in (f"%{term}%",) * 3]
    parameters.append(limit)
    with _connection(db_path) as connection:
        rows = connection.execute(
            f"SELECT * FROM meetings WHERE {conditions} "
            "ORDER BY created_at DESC LIMIT ?",
            parameters,
        ).fetchall()
    return [_decode_row(row) for row in rows]


def summarize_meeting(
    meeting_id: str, *, db_path: str | Path | None = None
) -> dict[str, Any]:
    """Return a structured, deterministic summary from saved meeting fields."""
    meeting = get_meeting(meeting_id, db_path=db_path)
    record = meeting["record"]
    return {
        "meeting_id": meeting["id"],
        "title": meeting["title"],
        "occurred_at": meeting["occurred_at"],
        "participants": meeting["participants"],
        "decisions": record["decisions"],
        "action_items": record["action_items"],
        "transcript": record["metadata"]["raw"].get("transcript")
        or record["metadata"]["raw"].get("summary")
        or record["metadata"]["raw"].get("notes")
        or record["metadata"]["raw"].get("content")
        or record["metadata"]["raw"].get("text"),
    }


def ask_meeting(
    meeting_id: str, question: str, *, db_path: str | Path | None = None
) -> dict[str, Any]:
    """Retrieve the most relevant transcript sentences using local keyword matching.

    This tool does not use an LLM; its response is evidence retrieval, not a
    generated explanation.
    """
    keywords = {
        word
        for word in re.findall(r"\w+", question.casefold())
        if len(word) > 2 and word not in STOP_WORDS
    }
    if not keywords:
        raise ValueError("Question must include a topic or keyword")
    meeting = get_meeting(meeting_id, db_path=db_path)
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", meeting["content"])
        if sentence.strip()
    ]
    matches = [
        (len(keywords & set(re.findall(r"\w+", sentence.casefold()))), sentence)
        for sentence in sentences
    ]
    relevant = [
        sentence
        for score, sentence in sorted(matches, key=lambda item: -item[0])
        if score
    ]
    return {
        "meeting_id": meeting_id,
        "question": question,
        "answer": "\n".join(relevant[:5]) if relevant else None,
        "matched_sentences": relevant[:5],
        "answer_type": "keyword_retrieval",
        "message": (
            "These are matching meeting excerpts, not an AI-generated explanation."
            if relevant
            else "No matching transcript excerpts were found."
        ),
    }


def export_meeting(
    meeting_id: str, *, db_path: str | Path | None = None
) -> dict[str, Any]:
    """Export a saved meeting as a JSON-serializable record."""
    return get_meeting(meeting_id, db_path=db_path)
