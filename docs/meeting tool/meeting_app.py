#!/usr/bin/env python3
"""A tiny local web app for the AURA meeting tool.

This app provides a simple browser-based interface that turns a raw meeting
payload into the same normalized structure used by the repository's
``normalize_meeting`` logic. It is intentionally lightweight: a local app shell
for the core tool, without depending on external infrastructure.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from meeting_tools import (
    ask_meeting,
    export_meeting,
    get_meeting,
    list_meetings,
    save_meeting,
    search_meetings,
    summarize_meeting,
)


HTML_PAGE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>AURA Meeting App</title>
    <style>
      :root {
        --bg: #0f172a;
        --panel: #111827;
        --panel-2: #1f2937;
        --text: #e5e7eb;
        --muted: #9ca3af;
        --accent: #7c3aed;
        --accent-2: #22c55e;
        --border: rgba(148, 163, 184, 0.2);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: linear-gradient(135deg, #020617, #111827 35%, #1e1b4b);
        color: var(--text);
      }
      .container {
        max-width: 1100px;
        margin: 40px auto;
        padding: 24px;
      }
      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 24px;
      }
      .brand {
        font-size: 1.6rem;
        font-weight: 700;
      }
      .layout {
        display: grid;
        grid-template-columns: 1.1fr 0.9fr;
        gap: 24px;
      }
      .panel {
        background: rgba(15, 23, 42, 0.82);
        border: 1px solid var(--border);
        border-radius: 18px;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
      }
      .panel h2 {
        margin: 0;
        padding: 18px 20px;
        border-bottom: 1px solid var(--border);
        font-size: 1.05rem;
      }
      .panel-body {
        padding: 20px;
      }
      label {
        display: block;
        margin-bottom: 10px;
        font-weight: 600;
        color: #d1d5db;
      }
      input, textarea, button {
        width: 100%;
        border-radius: 10px;
        border: 1px solid var(--border);
        padding: 12px 14px;
        font-size: 0.98rem;
      }
      input, textarea {
        background: rgba(17, 24, 39, 1);
        color: var(--text);
        margin-bottom: 16px;
      }
      textarea { min-height: 130px; resize: vertical; }
      .row {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
      }
      button {
        background: linear-gradient(135deg, var(--accent), #4f46e5);
        border: none;
        color: white;
        cursor: pointer;
        font-weight: 700;
        transition: transform 0.15s ease;
      }
      button:hover { transform: translateY(-1px); }
      button.secondary {
        background: rgba(34, 197, 94, 0.12);
        border: 1px solid rgba(34, 197, 94, 0.5);
        color: #d1fae5;
      }
      .output {
        white-space: pre-wrap;
        background: rgba(17, 24, 39, 0.95);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px;
        min-height: 300px;
        color: #dbeafe;
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
        overflow: auto;
      }
      .muted { color: var(--muted); }
      @media (max-width: 820px) { .layout { display: block; } }
    </style>
  </head>
  <body>
    <div class="container">
      <div class="topbar">
        <div class="brand">AURA Meeting App</div>
        <div class="muted">Works as a UI shell for the meeting tool</div>
      </div>

      <div class="layout">
        <section class="panel">
          <h2>Meeting Intake</h2>
          <div class="panel-body">
            <form id="meeting-form">
              <label>Meeting title</label>
              <input name="title" placeholder="Weekly product sync" />

              <div class="row">
                <div>
                  <label>Date</label>
                  <input name="date" placeholder="2026-10-25" />
                </div>
                <div>
                  <label>Participants</label>
                  <input name="participants" placeholder="Ava, Noah, Priya" />
                </div>
              </div>

              <label>Transcript / notes</label>
              <textarea name="transcript" placeholder="Paste the meeting transcript or notes here."></textarea>

              <label>Decisions (comma-separated)</label>
              <input name="decisions" placeholder="Ship report next Friday, Close Q3 backlog" />

              <label>Action items (comma-separated)</label>
              <input name="action_items" placeholder="Finalize summary, Share draft" />

              <p class="muted">Saving stores this record locally on this device.</p>
              <button type="submit">Save Meeting</button>
            </form>
          </div>
        </section>

        <section class="panel">
          <h2>Normalized record</h2>
          <div class="panel-body">
            <div id="output" class="output">{ "status": "ready" }</div>
          </div>
        </section>

        <section class="panel">
          <h2>Meeting tools</h2>
          <div class="panel-body">
            <label>Find saved meetings</label>
            <input id="search-query" placeholder="Search decisions, people, or topics" />
            <button id="search-button" type="button" class="secondary">Search meetings</button>
            <pre id="search-results" class="output">{ "meetings": [] }</pre>
            <label>Ask about a saved meeting</label>
            <input id="meeting-id" placeholder="Meeting ID from the saved record" />
            <input id="meeting-question" placeholder="What decisions were made?" />
            <button id="ask-button" type="button">Find relevant excerpts</button>
            <pre id="meeting-answer" class="output">{ "status": "ready" }</pre>
          </div>
        </section>
      </div>
    </div>

    <script>
      const form = document.getElementById('meeting-form');
      const output = document.getElementById('output');
      const searchResults = document.getElementById('search-results');
      const meetingAnswer = document.getElementById('meeting-answer');

      function csvToList(value) {
        return value
          .split(',')
          .map(item => item.trim())
          .filter(Boolean);
      }

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const formData = new FormData(form);

        const payload = {
          title: formData.get('title') || '',
          date: formData.get('date') || '',
          transcript: formData.get('transcript') || '',
          decisions: csvToList(formData.get('decisions') || ''),
          action_items: csvToList(formData.get('action_items') || ''),
          participants: csvToList(formData.get('participants') || ''),
        };

        output.textContent = 'Processing meeting...';

        try {
          const response = await fetch('/api/meetings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });

          const result = await response.json();
          output.textContent = JSON.stringify(result, null, 2);
          if (result.meeting?.id) {
            document.getElementById('meeting-id').value = result.meeting.id;
          }
        } catch (error) {
          output.textContent = JSON.stringify({ error: String(error) }, null, 2);
        }
      });

      document.getElementById('search-button').addEventListener('click', async () => {
        const query = document.getElementById('search-query').value.trim();
        const response = await fetch(`/api/meetings?query=${encodeURIComponent(query)}`);
        searchResults.textContent = JSON.stringify(await response.json(), null, 2);
      });

      document.getElementById('ask-button').addEventListener('click', async () => {
        const meetingId = document.getElementById('meeting-id').value.trim();
        const question = document.getElementById('meeting-question').value.trim();
        const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/ask`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question }),
        });
        meetingAnswer.textContent = JSON.stringify(await response.json(), null, 2);
      });
    </script>
  </body>
</html>
"""


class MeetingAppHandler(BaseHTTPRequestHandler):
    """Minimal browser app for the meeting tool."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML_PAGE)
            return
        if parsed.path == "/api/meetings":
            query = parse_qs(parsed.query).get("query", [""])[0]
            try:
                result = search_meetings(query) if query else list_meetings()
            except ValueError as exc:
                self._send_json({"status": "error", "message": str(exc)}, 400)
                return
            self._send_json({"status": "success", "meetings": result})
            return

        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if len(parts) in (3, 4) and parts[:2] == ["api", "meetings"]:
            meeting_id = parts[2]
            try:
                if len(parts) == 4 and parts[3] == "summary":
                    result = summarize_meeting(meeting_id)
                elif len(parts) == 4 and parts[3] == "export":
                    result = export_meeting(meeting_id)
                elif len(parts) == 3:
                    result = get_meeting(meeting_id)
                else:
                    self._send_json({"status": "error", "message": "Not found"}, 404)
                    return
            except LookupError as exc:
                self._send_json({"status": "error", "message": str(exc)}, 404)
                return
            self._send_json({"status": "success", "meeting": result})
            return
        self._send_json({"status": "error", "message": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if parsed.path not in ("/api/meeting", "/api/meetings") and not (
            len(parts) == 4 and parts[:2] == ["api", "meetings"] and parts[3] == "ask"
        ):
            self._send_json({"status": "error", "message": "Not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"status": "error", "message": "Invalid Content-Length"}, 400)
            return
        if length < 0 or length > 2_000_000:
            self._send_json({"status": "error", "message": "Request body must be at most 2 MB"}, 413)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"status": "error", "message": f"Invalid JSON: {exc}"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"status": "error", "message": "Request must be a JSON object"}, 400)
            return

        try:
            if len(parts) == 4 and parts[3] == "ask":
                result = ask_meeting(parts[2], payload.get("question", ""))
                self._send_json({"status": "success", **result})
            else:
                result = save_meeting(payload)
                self._send_json({"status": "success", "meeting": result}, 201)
        except ValueError as exc:
            self._send_json({"status": "error", "message": str(exc)}, 400)
        except LookupError as exc:
            self._send_json({"status": "error", "message": str(exc)}, 404)

    def _send_html(self, content: str):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, content: dict, status: int = 200):
        body = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):
        return


def run_server(host: str = "127.0.0.1", port: int = 8002):
    server = ThreadingHTTPServer((host, port), MeetingAppHandler)
    print(f"AURA Meeting App running at http://{host}:{port}")
    print("Use Ctrl+C to stop the app.")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
