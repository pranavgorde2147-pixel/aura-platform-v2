# AURA Daily Diary App – Android Studio Integration Guide

**App Slug:** `diary`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `timeline:write`,
`diary:write`
**Optional Permissions:** `records:read`, `records:write`, `records:delete`,
`diary:read`, `diary:delete`
**Ingestion Adapter:** `/api/v1/apps/diary/ingest` → `normalize_diary`

Diary text is persisted in the user's Server 2 Brain vault under
`apps/diary/`. Server 1 authenticates the account and authorizes the app; it
does not save new diary content in its PostgreSQL memory or app-record tables.
Server 2 must be reachable through the configured secure tunnel for ingestion
to succeed.

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "diary"}
```

## 2) Ingest diary entry
```json
POST /api/v1/apps/diary/ingest
Authorization: Bearer <access_token>
{
  "entry": "Today was peaceful. I spent time reading...",
  "mood": "calm",
  "date": "2026-10-21",
  "external_id": "diary-2026-10-21"
}
```
Requires `entry|text|content|body`. The response's `record_id` identifies the
vault record. Reuse the same stable `external_id` when retrying an offline
entry; it becomes the idempotency key, preventing duplicate diary records.
Keep the entry in the device's local pending queue until the API returns
`accepted: true`. A `503` means the entry was not confirmed as synchronized.
Optional keys: `mood` or `feeling`; `date`, `entry_date`, or `occurred_at`.
The adapter also accepts a stable dedupe identifier in the first available
key of `external_id`, `event_id`, `document_id`, `id`, `uuid`, or `reference`.
The ingestion response keys are `accepted`, `source_app`, `event_type`,
`duplicate`, `partial`, `memory_id`, `vault_entry_id`, and `record_id`.

## 3) Read and search diary records

Use only `https://aura-server1a-core.onrender.com/` and the authenticated
user's bearer token for protected requests. The Server 1 gateway proxies to
the user's Brain; never call Server 2 directly.

```http
GET /api/v1/brain/vault/records?source_app=diary&limit=50&offset=0
Authorization: Bearer <AURA user access token>
```

Diary search uses the owner's active, authorized app namespaces and shared
facts. To allow AURA Core or another
connected app to retrieve Diary entries, the user must keep both app
connections active and grant each `brain:read`. Cross-app search then includes
authorized namespaces automatically:

```http
POST /api/v1/brain/vault/search
Authorization: Bearer <AURA user access token>
Content-Type: application/json

{"source_app":"diary","query":"What was stressing me last week?","limit":10}
```

For a cross-app request, call `/api/v1/brain/query` with the caller's slug
(for example `source_app:"aura-core"`), not `source_app:"diary"`. Server 1
retrieves Diary records only while the owner's Diary connection has `brain:read`.

## 4) Query the Personal Brain

```json
POST /api/v1/brain/query
{"request_id":"<stable UUID for retries>","query":"What was my mood last week?","source_app":"diary","use_memory":true}
```

The Brain retrieves matching diary and shared-vault records from Server 2,
then sends the approved context to that user's Server 2 runtime. Queries and
answers are saved in the user's `apps/diary` namespace and are searchable to
connected apps authorized with `brain:read`. Query persistence is automatic;
legacy requests that send `persist_interaction:false` are still persisted.
If inference fails, the question remains in the private vault for retry. Reuse
the same `request_id` after a network timeout.

With `use_memory:true`, normal Brain queries and Vault search can use
read-only context from other active app connections granting `brain:read`;
document content also requires Files `files:read`. Show `records_used`
provenance clearly, and never present cross-app context as Diary-owned Vault
records.

For an immediate summary of the entry currently open in the app, include that
entry in the request context rather than sending only "summarize my entry":

```json
{
  "request_id":"<stable UUID for retries>",
  "query":"Summarize this diary entry",
  "source_app":"diary",
  "use_memory":true,
  "context":{"diary_entry":"<the user's current diary entry text>"}
}
```

Server 2 passes safe app context and retrieved authorized vault records to
the user's Brain runtime. It does not give the model unrestricted database
access; the model can only use the context supplied for that request.

When a diary entry is edited, use
`PUT /api/v1/brain/vault/records/{record_id}` with the latest `expected_version`.
Changing original diary text also requires `confirm_original_edit:true`.
Deletion is a confirmed soft-delete:
`DELETE /api/v1/brain/vault/records/{record_id}?source_app=diary&confirm=true`.
For all record operations, send `source_app=diary`. Manual updates require the
`memory:write` default grant; manual deletes require the additional
`diary:delete` grant and `confirm=true`. Do not silently add an optional
permission to an existing connection.

## 5) Diary Vault screen and harness

Provide a Diary-only Vault screen using
`GET /api/v1/brain/vault/records?source_app=diary&limit=50&offset=0`.
Paginate results and retain each record's `record_id` and `version`. The screen
must not list another app's namespace. For manual edits/deletes, use
`client_app=diary`, the latest `expected_version`, and a separate explicit
confirmation for original-content changes and deletion. Deletion requires the
additional `diary:delete` grant and is a soft-delete from retrieval.

For AI-assisted Diary changes, send the user's request to
`POST /api/v1/brain/query` with `source_app:"diary"` and a literal `harness:`
prefix and `use_memory:true`. The Brain may use other apps' authorized records
as read-only context, while harness tools remain limited to `apps/diary`.
Edits/creates may occur immediately. Render delete and bulk replacement plans
from top-level structured response fields and require a native confirmation
before submitting their plan IDs in a new request with the same `source_app`
and a new `request_id`. Never infer approval plans from assistant text.

## 6) Failure and privacy behavior

- The Android app should retain unsynchronized entries locally and retry with
  the same `external_id`; show a pending/sync-failed state on `503`.
- Account credentials and user-to-Brain mapping stay on Server 1. The shared
  Server 1-to-Server 2 service credential is server-side only and must never be
  bundled in the Android app.
- Diary entries, Brain memories, query/answer turns, and processed records are
  stored in Server 2's per-Brain vault. Do not use the legacy Server 1
  `/api/v1/records` endpoints for new diary content.
- A named Cloudflare Tunnel or another stable HTTPS hostname is required.
  Confirm `AI_SERVER_URL` and `STORAGE_SERVER_URL` point to the reachable
  Server 2 origin and keep the internal tokens configured only in server
  environments. Do not put these tokens in Android configuration.

## 7) Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Daily Diary app using this guide
as the API and behavior contract. Inspect the existing project, requirements,
architecture, auth, API client, local storage, and tests first; reuse the
existing patterns. If starting from an empty project, use native Kotlin,
Jetpack Compose, and Material 3 rather than a WebView or separate backend.

Create a clean, warm, calm, accessible diary experience with an entry
timeline, date navigation, mood/date details, a focused write/edit screen,
search/Brain questions, and a Diary-only Vault view. Provide useful loading,
empty, offline, retry, and error states. Keep unsynced entries in the app's
local queue and clearly show pending/synced/failed status.

Use only the Server 1 endpoints and exact `diary` payload fields documented
here. Connect by reconciling existing connections; handle 409 without a
reconnect loop. Generate a stable external_id before sync and reuse it with
the unchanged entry on retry. Brain queries/search should use the user's
authorized cross-app records as read-only context, keep source_app `diary`,
and display source attribution. Scope the Diary Vault UI and record
mutations to `apps/diary`, use record versions for edits, and obtain
confirmation for original-content changes and soft deletes. Harness may use
authorized cross-app context but its tools must remain Diary-only. Harness
deletion/bulk-change confirmation must use structured response fields, never
assistant prose.

Do not claim voice transcription, automatic timeline generation, or raw-data
retention unless an existing supported implementation is found. Do not call
Server 2 directly, bundle service secrets, or log diary text/tokens. Add
tests for entry editing, offline/idempotent sync, connection reconciliation,
Vault namespace isolation, permissions, and confirmation flows. Run the
available Android tests and debug build; report changes and results.
```
