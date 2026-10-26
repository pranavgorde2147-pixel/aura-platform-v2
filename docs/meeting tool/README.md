# AURA Meeting Tools

This first local MVP uses dedicated meeting tools rather than requiring the
Brain for record management. The browser app is an interface to those tools.

## Start the app

```bash
cd "docs/meeting tool"
python3 meeting_app.py
```

Open <http://127.0.0.1:8002>. Records are saved to
`~/.local/state/aura-meeting/meetings.sqlite3` by default. Set
`AURA_MEETING_DB` to use a different SQLite database path.

## Available tools

`meeting_tools.py` exposes functions for:

- `save_meeting(payload)` — normalize and persist a meeting record.
- `list_meetings()` and `get_meeting(meeting_id)` — browse saved meetings.
- `search_meetings(query)` — find records by transcript, title, or participants.
- `summarize_meeting(meeting_id)` — return stored decisions and action items.
- `ask_meeting(meeting_id, question)` — retrieve matching transcript excerpts.
- `export_meeting(meeting_id)` — return the complete JSON record.

Each function accepts an optional `db_path` argument, which is useful for tests
or for applications that need a separate database.

## App API

- `POST /api/meetings` — save a meeting payload.
- `GET /api/meetings` — list recent meetings.
- `GET /api/meetings?query=<text>` — search saved records.
- `GET /api/meetings/{id}` — retrieve a saved record.
- `GET /api/meetings/{id}/summary` — retrieve its structured summary.
- `POST /api/meetings/{id}/ask` with `{"question":"..."}` — retrieve relevant
  transcript excerpts.
- `GET /api/meetings/{id}/export` — export the saved record as JSON.

Question answering currently uses local keyword matching and returns source
excerpts; it does not generate an AI explanation or depend on the Brain. Live
capture from Google Meet, Zoom, or other calls is not implemented yet. This MVP
accepts meeting content supplied by the user; recording integrations need
explicit participant consent and platform-specific permissions.

Run the local tests with:

```bash
python3 -m unittest -v test_meeting_tools.py
```
