# AURA Meeting App – Android Studio Integration Guide

**App Slug:** `meeting`  
**Category:** Mobile (live)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `files:write`, `meeting:write`
**Optional Permissions:** `records:read`, `records:write`, `records:delete`,
`meeting:read`, `meeting:delete`
**Ingestion Adapter:** `/api/v1/apps/meeting/ingest` → `normalize_meeting` (FACT, `meeting.recorded`). Builds content from transcript + Decisions + Action items.

Meeting content is stored in the user's Server 2 Brain vault under
`apps/meeting/`, not in Server 1's Render content tables.

## 1) Auto-connect
After login, use `https://aura-server1a-core.onrender.com/` and the user's
bearer access token:
```kotlin
POST /api/v1/apps/me
Authorization: Bearer <access_token>
{"module_slug": "meeting"}
```
Omit `permissions` to request catalog defaults. Handle `201` as success and
`409` by refreshing `/api/v1/apps/me` rather than reconnecting repeatedly.
Permission updates at `/api/v1/apps/me/{connection_id}` replace the full
permission list. Keep only grants the user has approved.

## 2) Ingest meeting data
```json
POST /api/v1/apps/meeting/ingest
Authorization: Bearer <access_token>
{
  "title": "Sprint Planning",
  "transcript": "Discussed auth, rate limits, tests...",
  "decisions": ["Use JWT rotation", "Deploy to Render on merge"],
  "action_items": ["Add Redis cache", "Update API docs"],
  "participants": ["Alice", "Bob"],
  "occurred_at": "2026-10-21T09:00:00Z",
  "external_id": "meet-2026-10-21-sprint-01",
  "object_reference": "objects/transcript.txt"
}
```
Requires `transcript|summary|notes|content|text`. The response's `record_id`
identifies the Server 2 vault record. Reuse a stable `external_id` when
retrying; a key conflict returns `409`. Keep offline meeting data pending
until the Server 1 gateway confirms storage.
Optional input keys include `title`/`subject`, `decisions`, `action_items`/
`actions`/`follow_ups`, `participants`/`attendees`, `occurred_at`/`date`,
`object_reference`, `object_key`, `file_id`, `document_id`, `attachment`,
`storage_type`, `mime_type`, `file_size_bytes`, and `checksum`. Stable
dedupe IDs are read from `external_id`, `event_id`, `document_id`, `id`,
`uuid`, or `reference`. Response keys are `accepted`, `source_app`,
`event_type`, `duplicate`, `partial`, `memory_id`, `vault_entry_id`, and
`record_id`.

## 3) Query brain
```json
POST /api/v1/brain/query
Authorization: Bearer <access_token>
{
  "request_id": "<stable UUID for retries>",
  "query": "What were our action items from last meeting?",
  "source_app": "meeting",
  "use_memory": true,
  "memory_limit": 10
}
```

The Brain retrieves authorized Server 2 records and persists the query/answer
in `apps/meeting` by default. Use `POST /api/v1/brain/vault/search` to search
the owner's active, explicitly authorized app connections. Never call Server 2
or expose its service token from Android.
With `use_memory:true`, Brain queries/search can use read-only records from
active app connections with `brain:read` (and Files `files:read` for
documents). Show source attribution while keeping those records out of the
Meeting Vault and disabling Meeting edit/delete actions for them.

## 4) Verification
- [ ] Connected with `brain:read`
- [ ] Ingest with decisions/action_items returns 201, `record_id` present
- [ ] Query returns 200 with relevant context

## 5) Meeting-local Vault and harness

Add a Vault screen that lists only Meeting records through
`GET /api/v1/brain/vault/records?source_app=meeting&limit=50&offset=0`.
Paginate, retain `record_id` and `version`, and use
`client_app=meeting` for manual edit/delete requests. Require
`confirm_original_edit:true` only after the user confirms an original-content
change. Manual deletion requires the additional `meeting:delete` grant and an
explicit confirmation; it is a soft-delete from retrieval.

For AI-assisted changes, prefix the user's request with `harness:` and send it
to `/api/v1/brain/query` with `source_app:"meeting"` and `use_memory:true`.
The request can use authorized cross-app data as read-only context, while
harness tools remain limited to `apps/meeting`. Creates/edits may apply
immediately. Delete and bulk replacement plans must be confirmed in the app
using top-level structured response fields and a new request ID; never treat
assistant prose as a plan.

## 6) Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Meeting AI app using this guide
as the API and behavior contract. Inspect the existing project, requirements,
architecture, auth, storage, API clients, and tests; reuse its patterns. If
starting from an empty project, use native Kotlin, Jetpack Compose, and
Material 3, not a WebView or second backend.

Create a polished meeting workspace with a meeting list, meeting detail,
transcript/notes entry, clear sections for decisions and action items,
participants/time metadata, Brain Q&A, and a Meeting-only Vault. Make capture
and review workflows easy to scan, with loading, empty, offline, pending-sync,
and error states. Only implement device recording/transcription if the project
already supports it and all required runtime permissions and recording
consent are handled; do not invent server recording APIs.

Use only documented Server 1 routes and Meeting payload fields. Brain
queries/search should use authorized cross-app records as read-only context
when active connections grant `brain:read` (Files documents need
`files:read`); show sources without treating them as Meeting records. Connect
and reconcile the existing `meeting` grant set; retain pending records locally
and reuse a stable external_id on retries. Brain queries use source_app
`meeting`. Keep the Meeting Vault screen and all mutations scoped to
`apps/meeting`; harness may use authorized cross-app context but may only
manage Meeting records. Use versions for edits, and require confirmation for
original-content changes/deletes. Harness destructive plans must come from
structured response fields and be confirmed in-app.

Never call Server 2 directly or bundle its token. Protect transcripts,
participant data, and credentials from logs/analytics. Add tests for meeting
capture/save, action-item preservation, retry/idempotency, permissions,
namespace isolation, and confirmation flows. Run Android tests and debug
build; report changed files, results, and blockers.
```
