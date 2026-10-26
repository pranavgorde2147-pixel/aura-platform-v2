# AURA Notifications App – Android Studio Integration Guide

**App Slug:** `notifications`  
**Category:** Internal (planned)  
**Queries Brain:** `true`
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `notifications:read`, `notifications:write`
**Optional Permissions:** `notifications:delete`, `records:read`,
`records:write`, `records:delete`
**Ingestion Adapter:** `/api/v1/apps/notifications/ingest` → `normalize_notification` (stored in the user's Server 2 vault; no semantic-memory classification).

Use only `https://aura-server1a-core.onrender.com/` and the signed-in user's
bearer token. Notifications are marked `internal` and `planned`; the guide
does not mean a user-facing client is released.

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "notifications"}
```
The `brain:read` grant enables Brain queries, including authorized cross-app
read-only context; `memory:write` enables the app's own Vault management.
Vault tools and
Notifications-specific changes remain limited to its own namespace. Existing
connections do not receive new default grants retroactively; inspect
`/api/v1/apps/me` and have the user approve the additional permissions when
needed.

## 2) Ingest notification event
```json
POST /api/v1/apps/notifications/ingest
Authorization: Bearer <access_token>
{
  "title": "Build Succeeded",
  "message": "server1 deployed successfully",
  "level": "info",
  "source": "CI",
  "external_id": "notif-build-123"
}
```
Requires `message|body|text|content|title`. Returns 201 with `memory_id: null`
and a vault `record_id`. The event is stored under `apps/notifications/` in
Server 2, not in a Server 1 timeline table. Use a stable `external_id` and
retain pending events locally until a successful response.
Optional fields are `title`/`subject`, `level`/`severity`, and
`source`/`app`/`origin`; stable dedupe IDs may be `external_id`, `event_id`,
`document_id`, `id`, `uuid`, or `reference`. The full response keys are
`accepted`, `source_app`, `event_type`, `duplicate`, `partial`, `memory_id`,
`vault_entry_id`, and `record_id`. Notifications deliberately returns
`memory_id:null`; it stores an event record, not a personal fact.

## 3) Notifications Vault and harness

When Notifications is presented as a user-facing app, provide a Vault screen
that lists only
`GET /api/v1/brain/vault/records?source_app=notifications&limit=50&offset=0`.
Paginate results. Manual edits use the current `version`,
`expected_version`, `source_app=notifications`, and
`client_app=notifications`; original-content edits require an explicit user
confirmation. Manual deletion needs the additional `notifications:delete`
grant and an explicit confirmation, and is a soft-delete from retrieval.

For AI-assisted changes, send a user-approved request to
`POST /api/v1/brain/query` with `source_app:"notifications"` and the literal
`harness:` prefix and `use_memory:true`. The Brain may use other apps'
authorized records as read-only context, while harness tools can manage only
`apps/notifications`. Deletion and bulk-replacement plans must be rendered
from structured top-level response fields and confirmed in the app; never
interpret assistant prose as a plan.

## 4) Verification
- [ ] Connected with `brain:read` and `memory:write` grants
- [ ] Ingest returns `memory_id: null` and a vault `record_id`
- [ ] Brain queries use `source_app:"notifications"` and harness edits remain
      within `apps/notifications`

## 5) Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Notification Hub using this guide
as the API and behavior contract. Inspect the project, requirements,
architecture, auth, networking, local storage, and tests first. Reuse its
patterns; if starting from an empty project, use native Kotlin,
Jetpack Compose, and Material 3, not a WebView or a separate backend.

Create a clean notification-center UI with an event feed, readable event
details, source/level filters based only on available event fields, and a
Notifications-only Vault. Include loading, empty, offline, pending-sync,
permission, and retry states. Make event provenance clear and allow the user
to open the relevant source where a real link exists.

This backend currently stores notification activity records only. Do not
implement or advertise scheduled reminders, push delivery, snooze/dismiss
state, notification preferences, or reminder CRUD as server-backed features
unless the project has a separately verified service for them. Check current
catalog status before presenting this planned/internal app as released.

Use only documented Server 1 endpoints and `notifications` payload fields.
Keep events pending locally and retry the same stable external_id and payload.
Brain queries use source_app `notifications` and may use authorized cross-app
records as read-only context, with their source displayed. The Notifications
Vault screen and all mutations stay in `apps/notifications`. Require explicit
confirmation and structured plan fields for harness destructive operations.
Never call Server 2 directly,
bundle secrets, or log notification contents/tokens.

Add tests for event ingest/retry, `memory_id:null` handling, permissions,
namespace isolation, and structured confirmations. Run available Android
tests and debug build; fix regressions and report changes and results.
```
