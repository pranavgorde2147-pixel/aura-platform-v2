# AURA Travel App – Android Studio Integration Guide

**App Slug:** `travel`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `files:write`, `travel:write`
**Optional Permissions:** `records:read`, `records:write`, `records:delete`,
`travel:read`, `travel:delete`
**Ingestion Adapter:** `/api/v1/apps/travel/ingest` → `normalize_travel` (FACT, `travel.trip`)

Trip and itinerary records are stored in the user's Server 2 Brain vault under
`apps/travel/`; Server 1 handles authentication and app permission checks.
Use only `https://aura-server1a-core.onrender.com/` and the authenticated
user's bearer token; never call Server 2 or storage directly.

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "travel"}
```

## 2) Ingest trip/itinerary
```json
POST /api/v1/apps/travel/ingest
Authorization: Bearer <access_token>
{
  "destination": "Goa",
  "start_date": "2026-12-20",
  "end_date": "2026-12-27",
  "itinerary": "Beach, food, sightseeing",
  "external_id": "trip-goa-dec-2026",
  "object_reference": "objects/itinerary.pdf"
}
```
Requires `destination|destination_name|place|title`.
Use a stable `external_id` for retry-safe synchronization. List this app's
records with `GET /api/v1/brain/vault/records?source_app=travel`; search with
`POST /api/v1/brain/vault/search`. Search may include other connected app data
only when the owner has granted those apps `brain:read`.
Optional trip fields: `notes`, `itinerary`, `content`, or `summary`;
`start_date`/`departure`/`from_date`; `end_date`/`return_date`/`to_date`.
The adapter reads a stable ID from `external_id`, `event_id`, `document_id`,
`id`, `uuid`, or `reference`; object references may use `object_reference`,
`object_key`, `file_id`, `document_id`, or `attachment`. The response keys
are `accepted`, `source_app`, `event_type`, `duplicate`, `partial`,
`memory_id`, `vault_entry_id`, and `record_id`.

## 3) Query brain
```json
POST /api/v1/brain/query
{"request_id":"<stable UUID for retries>","query":"What trips do I have planned?","source_app":"travel","use_memory":true}
```
Queries and answers are persisted in the owner's `apps/travel` namespace by
default. Search may include another connected app's records only when the
owner has granted that app `brain:read`. Do not bundle or forward the Server 2
internal token from Android.
Normal Brain queries and Vault search can use cross-app records as read-only
context from active, authorized app connections (and Files documents with
`files:read`). Show source attribution while keeping those records out of the
Travel Vault and disabling Travel edit/delete actions for them.

## 4) Travel-local Vault and harness

Provide a paginated Travel Vault screen using
`GET /api/v1/brain/vault/records?source_app=travel&limit=50&offset=0`. It must
list only `apps/travel`. Use the authenticated Server 1 record get/update/
delete routes with `source_app=travel` and `client_app=travel`; edits use the
latest `expected_version` and require explicit confirmation when changing
original content. Manual delete needs the additional `travel:delete` grant and
an explicit user confirmation. It is a soft-delete from retrieval.

For AI-assisted Travel operations, send the user message with the literal
`harness:` prefix to `/api/v1/brain/query`, setting `source_app:"travel"`.
Set `use_memory:true` when cross-app read-only context is useful. The Brain
may use authorized records from other apps as context, but harness tools can
manage only `apps/travel`; this does not enable cross-app management.
Create/edit operations can happen immediately, while deletion and bulk
replacement return structured plans that require in-app confirmation. Read
those plans only from top-level response fields, and send approval/cancellation
IDs with the same source app and a new request ID.

## 5) Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Travel app using this guide as the
API and behavior contract. Inspect the project, requirements, existing
architecture, auth, persistence, networking, and tests first; preserve current
patterns. If starting from an empty project, use native Kotlin,
Jetpack Compose, and Material 3, not a WebView or a separate backend.

Create a visually clear trip planner with a trips overview, trip details,
destination and date editing, itinerary/notes, travel record history, Brain
Q&A, and a Travel-only Vault. Provide useful empty, loading, offline,
pending-sync, permission, and retry states. Keep trip drafts locally until
Server 1 confirms sync. Do not claim reservations, bookings, budgets,
checklists, maps, or reminders are server-backed unless the project contains
their real supported integration.

Use documented Server 1 routes and exact `travel` payload keys. Connect and
reconcile permissions; create a stable external_id and reuse it with unchanged
data on retries. Brain queries use source_app `travel` and may use authorized
cross-app records as read-only context with source attribution. Scope the
Travel Vault screen and all mutations to `apps/travel`, use optimistic
versions for edits, and require explicit confirmation for destructive changes.
For attachments, use
only documented Server 1 Files APIs and user-approved permissions; never call
object storage directly.

Harness may use authorized cross-app context but can only manage Travel
records. Harness delete/bulk plans must be read from structured top-level
response fields and confirmed in the app. Do not enable cross-app writes or
Core's whole-vault management mode.
Protect locations, itineraries, tokens, and secrets from logs. Add tests for
trip sync/retry, query permissions, Vault scope, version conflicts, and
confirmation behavior. Run Android tests and debug build; report changes and
results.
```
