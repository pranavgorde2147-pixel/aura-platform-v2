# AURA Health Android Connection and Integration Guide

**Exact catalog slug:** `health`
**Server:** AURA Server 1 on Render
**Catalog status:** `future` (confirm product readiness before release)
**Health event ingestion:** `POST /api/v1/apps/health/ingest`
**Vault namespace:** `apps/health`
**Default Permissions:** `brain:read`, `memory:write`, `notifications:read`,
`health:write`
**Optional Permissions:** `records:read`, `records:write`, `records:delete`,
`health:read`, `health:delete`

This guide is a complete implementation handoff for connecting an Android
Health app to the authenticated user's AURA Brain. Use the app's existing
networking and persistence architecture. Android communicates only with
Server 1; Server 1 authenticates the account, checks app permissions, and
forwards authorized data to that owner's Brain.

Health and medical data is highly sensitive. Connect and transmit only after
clear user consent. Ask separately before collecting wellness events,
reading health records into Brain answers, or retaining medical reports.
Never silently enable whole-vault access or tool writes.

## 1. Android Studio implementation prompt

Use the complete copy/paste prompt in section 12 after reviewing this
contract. It includes the native UI, privacy, server integration, and test
requirements for this app.

## 2. Render-only Server 1 configuration

Use this origin in every Android build:

```text
https://aura-server1a-core.onrender.com/
```

All paths below are relative to that origin. Use HTTPS and the existing
Retrofit/OkHttp (or equivalent) client. Configure a bearer-token interceptor
for protected calls, and keep the access/refresh tokens in the app's secure
token store. Refresh the access token using the existing auth flow when
needed.

Never call Server 2, `/v1/objects`, a tunnel, or a local service from Android.
Never send `INTERNAL_API_TOKEN`, `STORAGE_API_TOKEN`, Cloudflare credentials,
or `X-Aura-Owner-Id` from the app. Do not log bearer tokens, health payloads,
medical report text, or Brain record contents.

## 3. Account sign-in

Use the existing AURA account; Health does not create its own account.

```http
POST /api/v1/auth/login
Content-Type: application/json
```

```json
{"email":"user@example.com","password":"<entered securely>"}
```

For protected requests, send the returned token as:

```http
Authorization: Bearer <access_token>
```

Verify the signed-in session with `GET /api/v1/auth/me`. The current response
contains `id`, `user_id`, `email`, and `full_name`. Do not treat local cached
profile data as proof that the session is live.

## 4. Health catalog and connection

First inspect the current catalog and the signed-in user's existing
connections:

```http
GET /api/v1/apps
GET /api/v1/apps/me
Authorization: Bearer <access_token>
```

Find the exact `health` slug and inspect its returned `status` and
`default_permissions`. The current catalog declares:

- `brain:read` — let the Health app ask the user's Brain questions.
- `memory:write` — required by the Health ingestion adapter for submitting
  consented events and report text.
- `notifications:read` — only needed if an actual Health feature reads
  notifications; do not request it unnecessarily.
- `health:write` — catalog grant for Health data/routing; request it when
  needed by the product's approved behavior.

The catalog currently labels Health as `future`, with a description requiring
product confirmation. Confirm that launch is approved before enabling Health
connect/ingestion in a public release.

After presenting the consent explanation, create the connection:

```http
POST /api/v1/apps/me
Authorization: Bearer <access_token>
Content-Type: application/json
```

Request the catalog defaults only if the user has approved that complete
permission set:

```json
{"module_slug":"health"}
```

For a narrower grant, send only the permissions this user-approved feature
needs. For example, a Health client that both submits consented events and
asks Brain questions can request:

```json
{
  "module_slug":"health",
  "permissions":[
    {"scope":"brain","action":"read"},
    {"scope":"memory","action":"write"}
  ]
}
```

The server checks requested permissions against the catalog allowlist; the
permission list is the complete grant set, not an additive patch. The Health
ingestion route specifically enforces `memory:write`; `brain:read` is needed
for Health-originated Brain queries. Add `health:write` only when the feature
uses Health's approved routed-write behavior. Avoid `notifications:read`
unless the app actually needs it.

Handle responses:

- `201 Created`: retain the returned connection `id`, `module_slug`,
  `is_active`, and `permissions`.
- `409 Conflict`: call `GET /api/v1/apps/me`, locate `module_slug == "health"`,
  and inspect/reactivate/update the existing user-owned connection. Do not
  repeatedly POST or delete/reconnect.
- `401`: refresh token or ask the user to sign in.
- `403`: show a permission/consent explanation; do not retry unchanged.
- `404`: check catalog status and exact slug.

Update an existing connection using `PATCH /api/v1/apps/me/{connection_id}`.
If permissions are supplied, send the complete new set of grants to preserve.
To deactivate or revoke, use:

```http
PATCH /api/v1/apps/me/{connection_id}
Content-Type: application/json

{"is_active":false}
```

or, after clear user confirmation:

```http
DELETE /api/v1/apps/me/{connection_id}
```

Refresh `GET /api/v1/apps/me` and reflect the server's actual result in the UI.

## 5. Submit a consented wellness event

Before collecting/sending, explain what will be shared, why, and that it will
be stored in the user's private Brain vault. Capture consent in the app UI;
the API does not provide a consent screen or infer consent automatically.
Persist each unsent event in the app's private local database with a stable
client-generated ID before attempting the request.

```http
POST /api/v1/apps/health/ingest
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "event":"run",
  "content":"Ran 5 km in 28 minutes",
  "value":5,
  "unit":"km",
  "occurred_at":"2026-10-21T07:00:00Z",
  "external_id":"<stable-client-generated-event-id>"
}
```

Supported event text fields include `content`, `notes`, `summary`, `text`,
`message`, or `value`. Optional metadata includes `event` (or `metric`,
`activity`, `type`), `value`, `unit`, and `occurred_at` (or `date`,
`timestamp`). Supply a stable `external_id` (also accepted: `event_id`,
`document_id`, `id`, `uuid`, or `reference`) for retry-safe idempotency. Keep
the same ID and same content for retries. The response keys are `accepted`,
`source_app`, `event_type`, `duplicate`, `partial`, `memory_id`,
`vault_entry_id`, and `record_id`.

On `201 Created`, mark the local item synced and retain the returned
`record_id`. Repeated submissions with the same stable ID and identical
content are reported as duplicates; reuse of an ID for different content
returns `409 Conflict` and must be reconciled, not silently treated as success.

## 6. Submit locally extracted report text

For a report, request consent to process the document and send its extracted
text. Extract text locally and send only what the feature needs:

```http
POST /api/v1/apps/health/ingest
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "document_id":"<stable-client-generated-document-id>",
  "file_name":"lab-results.pdf",
  "file_type":"application/pdf",
  "extracted_text":"<user-approved extracted report text>",
  "extraction_method":"android-document-parser"
}
```

Document fields also accept `extracted_content` instead of
`extracted_text`, `document_name`/`filename` instead of `file_name`, and
`document_type`/`mime_type` instead of `file_type`. The event time may be
`occurred_at`, `date`, or `timestamp`.

Use a stable `document_id` and retry the same report content with that ID.
The extracted report text becomes the content of an owner-scoped vault record
under `apps/health`; it is not sent to Server 1's Render relational content
tables. Do not put the full report text in diagnostics, crash reports, or
analytics.

The Health ingestion endpoint accepts JSON, not the original binary. If the
user explicitly wants the original PDF/image retained, ask separately and use
`POST /api/v1/files` through Server 1. File upload requires an active Files
connection with `files:write`; because Health's defaults do not include that
grant, do not attempt upload until the user has connected Files and approved
the required permission. Avoid storing both file and extracted text twice
unless the product explicitly needs both.

## 7. Query the Personal Brain

Only offer Brain questions after the Health connection is active and the
server-confirmed grants include `brain:read`:

```http
POST /api/v1/brain/query
Authorization: Bearer <access_token>
Content-Type: application/json
```

```json
{
  "request_id":"<stable UUID for this question>",
  "source_app":"health",
  "query":"How active was I this week?",
  "use_memory":true,
  "persist_interaction":true,
  "manage_vault":false,
  "confirm_tool_writes":false
}
```

Create a new UUID for each new question and reuse it only to retry that same
question. Queries use the authenticated owner's data; default retrieval
respects active app connections and their Brain-read grants. Do not set
`manage_vault` or `confirm_tool_writes` for ordinary Health queries. A `403`
means the user must reconnect or grant the required permission. For `503`,
keep the question and request ID available for a manual retry and show the
safe server error instead of generating a fake answer.

## 8. Read Health records

List structured Health records through the Server 1 Brain Vault gateway:

```http
GET /api/v1/brain/vault/records?source_app=health&limit=50&offset=0
Authorization: Bearer <access_token>
```

This reads the authenticated user's `apps/health` namespace and requires the
Health app's active connection and appropriate Brain read permission. Do not
use record IDs as authorization. The Health Vault screen lists only Health
records. Separately, normal Brain queries and Brain search can use read-only
context from other apps whose active connections grant `brain:read`; document
content additionally requires Files `files:read`. Keep `source_app:"health"`,
display source attribution, and do not present those results as Health-owned
records.

## 9. Edit or delete Health vault records

Health can edit records in its own `apps/health` namespace when its active
connection grants `memory:write` (included in the catalog defaults). Retrieve
the current record and version before showing an edit form. Updates use
optimistic concurrency; changing the original content requires an explicit
confirmation:

```http
PUT /api/v1/brain/vault/records/{record_id}?source_app=health&client_app=health
Authorization: ******
Content-Type: application/json

{"expected_version":2,"content":"User-approved update","confirm_original_edit":true}
```

Deletion is a separately consented, soft-delete operation, not an automatic
part of editing. Request the additional `health:delete` permission from the
user and include it in the complete permission list when updating the Health
connection. Then send `DELETE /api/v1/brain/vault/records/{record_id}?source_app=health&client_app=health&confirm=true`.
The Health catalog is still `future`; enabling these controls does not make
Health generally available.

Through the direct Vault gateway, AURA Core edits require Core `memory:write`
and an active Health connection with `brain:read`; direct deletes also require
Health's `health:delete` grant and explicit delete confirmation. Core's
separate Whole-Vault Mode can manage Health records through Brain tools after
the owner explicitly enables it and confirms each destructive plan. Health
data must remain user-consented and may be used as read-only context for
another app only while the owner has authorized that app connection.

For AI-assisted Health changes from within the Health app, send the user's
message to `/api/v1/brain/query` with `source_app:"health"` and a literal
`harness:` prefix and `use_memory:true`. The request can use authorized
cross-app records as read-only context, while harness tools can manage only
`apps/health`. Creates and edits may apply immediately. Deletion and
bulk-replacement plans must be shown from structured top-level response
fields and confirmed in the Health app before sending an approval ID with a
new request ID. AURA Core's cross-app management flow below is separate and
requires its explicit whole-vault preference.

For AI-managed deletion from AURA Core chat, use the separate Brain-management
tool confirmation flow: read `pending_delete_plans` from the Server 1 response
and show the exact `apps/health` namespace, record count, expiry, and permanent
impact. After the user confirms, send a new Brain query with
`delete_approval_id` set to the returned `plan_id` and a new `request_id`.
Cancel by sending `delete_plan_cancel_id` with the plan ID, preserving the
original `source_app` and `manage_vault` value. Plans are
owner/Brain/source-app scoped, bound to the exact record-ID set, expire after
five minutes, and can be used once. Check `delete_result` and refresh the vault
before reporting completion. This permanently removes Health vault records
and Brain-side versions/indexes. It does not remove original binaries stored
through Files. The old `confirm_tool_deletes`, scope, and count fields are not
an approval mechanism. AI-managed edits replace current content without
retaining prior versions.

## 10. Errors, offline sync, and privacy

- `400`/`422`: show the safe validation detail and keep the item editable.
- `401`: refresh the access token or ask the user to sign in again.
- `403`: explain that Health is not connected, active, or granted the required
  permission.
- `404`: verify that the catalog has the `health` slug and ingestion adapter.
- `409`: reconcile the same ID; do not silently mark the event synced.
- `413`/`415`: apply the Server 1 file size/type constraints if uploading an
  original file.
- `503`, timeout, or connectivity failure: retain the pending event and retry
  with the same stable ID and unchanged content.

Use WorkManager or the app's existing offline queue with network constraints.
Never mark an event synchronized until Server 1 returns success. Redact
medical content and all credentials from logs. Allow users to revoke the
connection and manage locally pending data; do not imply that revoking an app
connection automatically deletes records already saved in the Brain.

## 11. Verification checklist

- [ ] The app uses only `https://aura-server1a-core.onrender.com/`.
- [ ] The user signs in with their existing AURA account.
- [ ] The app checks the catalog and existing connections before connecting.
- [ ] Health is presented as `future` until product readiness is confirmed.
- [ ] Consent is obtained before connecting or sending sensitive health data.
- [ ] Connection response grants are shown and match the requested permissions.
- [ ] A `409` refreshes `/api/v1/apps/me` instead of looping.
- [ ] Event ingestion returns `201` and a record ID in a dedicated test account.
- [ ] Offline retries reuse an unchanged payload and stable event/document ID.
- [ ] Brain queries are sent only with the active Health `brain:read` grant.
- [ ] File retention is separately consented and uses only Server 1 `/files`.
- [ ] Logs contain no health text, report content, tokens, or service secrets.
- [ ] Revocation stops future Health operations without claiming old records
      were erased.
- [ ] Health record edits use the current `expected_version`; original-content
      edits and soft deletes require explicit user confirmation.
- [ ] Android unit tests and debug build pass; report exact results.

## 12. Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Health app using this guide as the
API, privacy, and behavior contract. First inspect the project, requirements,
architecture, auth, storage, API client, and tests; preserve existing patterns.
If the project is empty, use native Kotlin, Jetpack Compose, and Material 3.
Do not use a WebView or create a new backend.

Create a calm, accessible health UI with a clear consent/onboarding flow,
wellness-entry form and history, health-only Vault, and Brain question screen.
Keep screens simple and reassuring, with explicit loading, empty, sync-pending,
retry, permission-denied, and error states. Do not present generated answers
as diagnoses or emergency advice.

Health is currently marked `future`: do not represent the app as generally
available or enable transmission until product readiness is approved. Obtain
explicit consent before connecting, sending each health event/report, reading
records into Brain answers, or retaining an original file. Use only documented
Server 1 routes and exact `health` payload fields. Keep stable event/document
IDs and unchanged payloads for retries. Do not upload original binaries to
the Health ingestion route.

Allow normal Brain questions to use cross-app context only through active
app grants and health-data consent; clearly show source attribution. Keep the
Health Vault screen and every edit/delete action inside `apps/health`; use
current versions for edits and separate user confirmation for original-content
changes and deletion. A `harness:` request may use authorized cross-app records
as read-only context, but tools must remain Health-only. Harness plans must
come from structured top-level response fields and require confirmation in
the app. Never enable cross-app writes or Core's whole-vault management mode
from Health.

Never call Server 2 or storage directly. Never log health data, tokens, or
secrets; do not send private data to analytics. Add tests for consent gates,
connection/permission reconciliation, stable-ID offline retry, Vault
isolation, query authorization, and confirmation flows. Run Android tests
and the debug build; fix regressions and report changed files and results.
```
