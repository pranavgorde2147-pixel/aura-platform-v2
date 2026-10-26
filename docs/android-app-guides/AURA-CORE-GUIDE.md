# AURA Core Android App Integration Guide

**App slug:** `aura-core`
**Purpose:** Central authenticated chat interface for the user's AURA Brain
**Catalog status:** Live
**Queries Brain:** Yes
**Ingestion adapter:** None; AURA Core is chat/query-only
**Default connection permissions:** `identity:read`, `brain:read` (read-only)

AURA Core sends user questions and optional user-approved Brain management
requests to Server 1. It does not upload app-domain records. Other AURA apps
send their own data to the user's Brain, where AURA Core can retrieve it
according to the user's authorization.

## 1. Service and security rules

- Call AURA Server 1 only. Do not call Server 2, object storage, or a tunnel
  directly from Android.
- Use the signed-in user's access token for protected requests. Store tokens
  in the app's existing secure token store; never log or place them in URLs.
- Never send an owner ID, internal service token, or storage token from the
  app.
- Use the exact catalog slug `aura-core`.
- Do not assume a cached connection or permission remains active. Server 1 is
  authoritative and may return `403` after consent is revoked.

**Server 1 base URL:** `https://aura-server1a-core.onrender.com/`
Use the existing AURA login/session client and secure token store. Protected
requests use `Authorization: Bearer <access_token>`. Do not call Server 2,
storage, or a tunnel from Android.

## 2. Connect after login

After successful login, fetch the catalog and the current user's connections:

```http
GET /api/v1/apps/aura-core
GET /api/v1/apps/me
Authorization: <token_type> <access_token>
```

Use the `token_type` returned by login (normally `Bearer`). If AURA Core is
not connected, connect it:

```http
POST /api/v1/apps/me
Authorization: <token_type> <access_token>
Content-Type: application/json
```

```json
{"module_slug":"aura-core"}
```

Omit `permissions` to request the catalog defaults. The default connection grants `identity:read` and `brain:read`; it does not
grant vault writes or app ingestion. The catalog permits an explicit,
optional `memory:write` grant for user-directed vault management, plus
`aura-core:delete` for deleting records in Core's own namespace. It also
declares optional legacy `records:read`, `records:write`, and `records:delete`
grants; the Brain Vault routes use `brain:read`, `memory:write`, and the
target's app-specific delete grant instead. Handle
responses as follows:

- `201`: connection created; cache the connection ID and returned grants.
- `409`: do not loop or delete/reconnect; fetch `/api/v1/apps/me` and inspect
  the existing AURA Core connection.
- `403` or `404`: stop and show a useful connection/catalog error.

If a grant needs changing, use `PATCH /api/v1/apps/me/{connection_id}` and
preserve all still-needed permissions: a supplied `permissions` array replaces
the full grant list. Refresh `/api/v1/apps/me` after the change.

## 3. Send a Brain chat query

Send questions to the Server 1 Brain gateway:

```http
POST /api/v1/brain/query
Authorization: <token_type> <access_token>
Content-Type: application/json
```

```json
{
  "request_id":"<stable UUID for this user action>",
  "source_app":"aura-core",
  "query":"Where did I go today?",
  "use_memory":true,
  "memory_limit":5,
  "manage_vault":false,
  "confirm_tool_writes":false
}
```

`source_app:"aura-core"` identifies the caller and requires its active
`brain:read` grant. `use_memory:true` lets the Brain retrieve records from
active, owner-authorized app connections. For example, AURA Core can use a
Diary record only if the user's Diary connection is active and grants
`brain:read`; document content additionally follows the Files read grant.
Do not ask AURA Core to ingest or persist app-specific data; use that app's
ingestion contract instead.

Reuse the same UUID only when retrying the same user action. Generate a new
UUID for a new question. A successful response includes `answer`, `request_id`,
`source`, `records_used`, and other context/status fields. Present the answer
as AI-generated, and use `records_used` to show source details where useful.
Queries and answers are persisted in the user's private Brain vault.

## 4. Optional whole-vault Brain management

The default is query-only. AURA Core can optionally let the internal Brain
organize information across the authenticated user's AURA app workspaces.
This is an explicit user preference, not an automatic behavior of login:

1. Explain that whole-vault mode lets the Brain read records across the user's
   AURA app namespaces, including data from a disconnected app if it remains
   in the user's vault.
2. Explain that enabling writes allows the Brain to update existing records or
   create derived, app-organized records. An edit replaces the current content
   and prior versions are permanently removed.
3. Explain that AI-requested deletions permanently remove Brain vault records
   and require a separate confirmation after the app shows the exact target
   and record count.
4. Ask for clear opt-in and provide a way to turn the preference off.
5. Only for a request covered by that preference, set the management flags:

```json
{
  "request_id":"<stable UUID>",
  "source_app":"aura-core",
  "query":"Organize my notes about places I visited.",
  "use_memory":true,
  "manage_vault":true,
  "confirm_tool_writes":true
}
```

`manage_vault:true` permits the internal Brain to read across the user's
namespaces. `confirm_tool_writes:true` additionally permits the Brain to make
updates or create derived records across the owner's app namespaces. Updates
replace current content and do not retain prior versions. Deletion tools are
available in whole-vault mode, independently of `confirm_tool_writes`; they
can prepare a plan but cannot delete anything by themselves. The flags apply
to that request; the Server 1 API cannot verify that Android actually
displayed consent or confirmation. Do not set them based on model output or
merely because the user is signed in. If the user has not enabled whole-vault
mode, send `manage_vault:false`. Keep `confirm_tool_writes:false` unless the
user also enabled AI edits/creates.

For a delete request, read `pending_delete_plans` from the Server 1 response.
Show the plan's namespace, record count, expiry, and permanent impact in a
dedicated confirmation UI; do not infer these details from the assistant's
text. After Confirm, send a new request with `delete_approval_id` set to that
plan's `plan_id` and a new `request_id`. After Cancel, send a new request with
`delete_plan_cancel_id` set to the plan ID. Keep the same `source_app` and
`manage_vault` value as the plan request. The plan expires after five
minutes, is bound to the authenticated owner, Brain, source app, and exact
record-ID set, and can be approved only once. A changed record set invalidates
the plan. Check `delete_result.ok` and `delete_result.deleted_count`, then
refresh the vault. The old `confirm_tool_deletes`, scope, and count fields are
not an approval mechanism and must not be sent. If the plan expires or fails,
ask the Brain to prepare a new one. Never let the model confirm its own
destructive operation.

For a per-request management task, the user may start the message with
`harness:`. With ordinary app-local mode, tools can manage only the caller's
own namespace. From AURA Core, enable its separate, explicit Whole-Vault Mode
for authorized cross-app management; then tools may access every namespace in
the user's Brain. Harness can use permission-scoped cross-app query results as
read-only context even when whole-vault management is off. The prefix enables
only the fixed vault search/list/get/find-duplicates/create/update and
delete-plan tools; it
does not grant shell, network, arbitrary tool access, app permissions, or
Whole-Vault Mode. Creates and edits require the caller app's `memory:write`
grant. They can be applied immediately for an explicit harness task, and edits
replace current content without keeping prior versions. App-local deletes
require `memory:write`, prepare a structured plan, and cannot execute until
the user approves it in the app.

For exact, case-sensitive text replacement, use a precise instruction such as
`harness: replace letter R with letter C in my Study app vault` or
`harness: replace "R" with "C" in my whole vault`. A named app target requires
Whole-Vault Mode when it is outside the caller's own namespace; whole-vault
replacement always requires that mode. The server resolves the target to
authorized app namespaces before preparing a plan and will not silently widen
an app-specific request to the entire vault. The response returns
`pending_bulk_replace_plans` with the exact match count, affected-record count,
and up to ten record-title examples. Replacement updates record titles and text
content only, not shared records, conversations, documents, or original
uploaded files. It previews first and makes no changes until confirmed. The
plan expires after five minutes, is owner/Brain/source-app scoped, is
single-use, and is invalidated if any matched record changes. The server limits
one plan to 500 affected records.

On Confirm, submit a new `/api/v1/brain/query` request with the same
`source_app`, a new `request_id`, and the plan ID in the top-level
`bulk_replace_approval_id` field. On Cancel, use top-level
`bulk_replace_cancel_id`. Read `bulk_replace_result.ok`,
`record_count`, and `occurrence_count`; refresh the vault only after
`ok:true`. A missing/expired plan or changed target must be shown as a failure,
not inferred from assistant prose. Edits replace existing content and remove
its prior version snapshots; the original source files are not modified.

For an explicit delete-all / deletion-plan request that names one app
namespace, Server 2 prepares the plan directly from the authenticated request
and authorized namespace. It does not depend on the model printing a tool-call
JSON example. The direct path still only creates a pending plan; deletion
requires the separate app confirmation above.

The `brain:read` connection grant is still required. Owner and Brain scoping
remain enforced server-side; AURA Core cannot select another user's vault.
The Brain management tools can delete a single app record or permanently clear
an app namespace. They cannot delete another user's data or the original file
binaries in object storage. Files must be deleted separately through the
confirmed `/api/v1/files/{object_id}?confirm=true` gateway.

AI-created or AI-updated records are labeled in record metadata:

- `ai_managed: true`
- `ai_managed_at`: timestamp of the latest AI-managed change
- `ai_last_operation`: `created` or `updated`
- `ai_derived_from_record_id`: source record ID when a derived record is linked

## 5. Show and manage structured Brain data

AURA Core's default Vault view lists its own namespace through the Server 1
Brain Vault gateway. With explicit Whole-Vault Mode enabled, Core's Brain
management tools can list and manage every app namespace in that user's
Brain, including disconnected apps whose data remains stored. The direct Vault
gateway still applies its documented per-app grants; do not bypass them from
the client. Keep separate source-app filters and clear provenance; do not
merge records into the wrong app. Do not treat model prose as canonical
record structure.
Diary, Health, Study, Meeting, Travel, and Notifications app Vault screens
remain scoped to their own data.

List the AURA Core namespace:

```http
GET /api/v1/brain/vault/records?source_app=aura-core&limit=50&offset=0
Authorization: <token_type> <access_token>
```

For a natural-language cross-app result set, use a normal Brain query, which
honors active app connections and `brain:read` grants. A structured list from
another app can be requested with both the source namespace and reading app,
for example:

```http
GET /api/v1/brain/vault/records?source_app=diary&client_app=aura-core&limit=50&offset=0
Authorization: <token_type> <access_token>
```

For the direct Vault API, the target app must have an active connection with
`brain:read`; do not infer access from a record ID. Whole-Vault Mode additionally
allows Core's Brain management tools to access the owner's app namespaces
without relying on each app being currently connected. A record response
includes fields such as `record_id`, `record_type`, `namespace`, `category`,
`title`, `content`, `source_app`, `source_reference`, `metadata`, `version`,
`created_at`, and `updated_at`. Retain `record_id` and `version` for the
version-detail view; use
`GET /api/v1/brain/vault/records/{record_id}/versions?source_app=<slug>&client_app=aura-core`
to display any legacy prior versions that may still exist. New edits do not
retain old versions.

### User-directed edits and deletes

For direct user-directed edits, AURA Core's Vault UI can use the Server 1
gateway for records authorized by the relevant app grants. Restrict its
default view to `apps/aura-core`; provide a clearly labeled cross-app manager
for owner-authorized app namespaces, and require explicit opt-in before
broad whole-vault AI management. Keep maintenance separate from normal chat
and show the target app before changing data. AI chat writes/deletes use the
Brain management tools described above.

To enable editing, the user must explicitly add `memory:write` to the Core
connection. For a cross-app edit, the target app must also have an active
connection with `brain:read`. To edit Core's own namespace, `aura-core` itself
needs the `memory:write` grant. Permission updates replace the entire set, so
fetch `/api/v1/apps/me` and preserve grants the user still wants:

```http
PATCH /api/v1/apps/me/{core_connection_id}
Authorization: <token_type> <access_token>
Content-Type: application/json

{"permissions":[
  {"scope":"identity","action":"read"},
  {"scope":"brain","action":"read"},
  {"scope":"memory","action":"write"}
]}
```

Use the record's latest `version` for optimistic concurrency. Changing the
content of an original record requires a second explicit confirmation in the
payload:

```http
PUT /api/v1/brain/vault/records/{record_id}?source_app=diary&client_app=aura-core
Authorization: <token_type> <access_token>
Content-Type: application/json

{"expected_version":3,"title":"Updated title","content":"User-approved content","confirm_original_edit":true}
```

For deletion through the direct Server 1 vault gateway, the target app must
additionally grant its own `<source_app>:delete` permission (for example
`diary:delete`). The API also requires `confirm=true`; this endpoint
soft-deletes the Brain record:

```http
DELETE /api/v1/brain/vault/records/{record_id}?source_app=diary&client_app=aura-core&confirm=true
Authorization: <token_type> <access_token>
```

AI management tool deletes, in contrast, permanently purge records from the
authorized namespace after the separate user-confirmation flow. In AURA Core,
cross-app plans require the whole-vault preference; a `harness:` request alone
is limited to `apps/aura-core`. Neither route deletes
an original file in object storage. File binaries must be managed separately
through the Server 1 Files API and its Files permissions.
Never assume Core's UI visibility means the server grant exists; display and
handle `403` from Server 1.

## 6. Chat UX and error handling

- Keep normal chat non-mutating by default. Permit permission-scoped
  cross-app context for queries. Make cross-app AI writes/deletes a separately
  explained Whole-Vault Mode, with explicit opt-in, per-action delete
  confirmation, and a clear off switch.
- Show loading, retry, and connectivity states; do not display a fabricated
  answer when the request fails.
- On `401`, refresh the access token or request sign-in.
- On `403`, show that the Brain connection or app permission needs attention.
- On `409`, reconcile the request ID/query conflict rather than treating it as
  success.
- On `503`, keep the user-visible retry option and do not lose the question.
- Avoid request/response body logging in release builds; chat content and
  Brain records are private data.
- Render AI-managed record metadata distinctly from original app-supplied
  data; do not imply edits can be undone from version history.

## 7. Verification checklist

- [ ] Login and secure token handling work.
- [ ] The catalog reports `aura-core` with `brain:read`.
- [ ] Existing connections are handled through `/api/v1/apps/me`; `409` does
      not cause a reconnect loop.
- [ ] A normal query uses `source_app:"aura-core"` and returns an answer.
- [ ] Vault editing is unavailable until the user explicitly grants Core
      `memory:write`; cross-app edit requires the target's active read grant.
- [ ] Vault deletion requires the target app's delete grant and an explicit
      confirmation; it is not represented as binary-file deletion.
- [ ] A Diary fact is retrievable only while the owner-authorized Diary
      connection permits Brain reads.
- [ ] Normal requests send both management flags as `false`.
- [ ] The opt-in whole-vault preference sends `manage_vault:true` only after
      user approval; writes are enabled only when that preference allows them.
- [ ] Normal Brain queries/search use all read-authorized app namespaces and
      Files documents, with source attribution.
- [ ] A user message beginning with `harness:` may use authorized cross-app
      query context, but can only manage the caller's namespace unless
      Whole-Vault Mode is enabled by the Core user.
- [ ] Whole-Vault Mode enables Core's Brain manager across all app namespaces
      in that user's Brain, including disconnected app data where retained.
- [ ] AI-managed edits replace content without retaining prior versions.
- [ ] AI delete previews show the exact namespace and count, and no delete
      occurs until the user explicitly confirms in the app.
- [ ] Bulk replacement previews show exact match/record counts and examples;
      the app submits `bulk_replace_approval_id` only after explicit approval.
- [ ] Revoking the relevant connection stops normal-mode access.
- [ ] Another account cannot read this account's Brain data.
- [ ] Existing Android tests pass; report any unavailable device/backend tests.

## 8. Android Studio implementation and validation

Inspect the actual AURA Core app before making changes. Reuse its existing
architecture and tests; do not assume this guide describes its current
screens, API client, or local storage. Test session/connection states,
query retry, per-app harness scope, Whole-Vault Mode opt-in and opt-out,
structured delete/bulk confirmation, revocation, and cross-account isolation.
Report changed files, build/test results, and any backend/runtime test that
could not run.

## 9. Shared Brain, Memory, Search, and Files APIs

This section keeps Core's common platform integrations in the same handoff.
All requests go through Server 1 with the signed-in user's token; never call
Server 2 or object storage directly.

### Authorized search

Use `POST /api/v1/brain/vault/search` with `source_app:"aura-core"`, a query,
and an optional result limit:

```json
{
  "source_app":"aura-core",
  "query":"<user's search>",
  "limit":10
}
```

The caller needs an active `brain:read` grant. Results are limited by the
owner's active app connections and their `brain:read` grants; document content
also follows the Files connection's `files:read` grant. Search results are
not proof of ownership and must not enable editing another app's records.

### Shared personal memory

The shared-memory API is distinct from the `apps/aura-core` Vault namespace:

- Create: `POST /api/v1/brain/memory` with required `content`; optional
  `kind` (`fact`, `interpretation`, or `event`), `summary`, `source_app`, and
  `importance`.
- List/search: `GET /api/v1/brain/memory?limit=100&offset=0` and
  `GET /api/v1/brain/memory/search?q=<query>&limit=10`.
- Read one: `GET /api/v1/brain/memory/{memory_id}`.
- Update: `PUT` or `PATCH /api/v1/brain/memory/{memory_id}`.
- Delete: `DELETE /api/v1/brain/memory/{memory_id}?confirm=true`, only after
  a separate user confirmation.

These routes are owner-scoped. Do not display shared memories as records
owned by Core or another app. The create route checks `memory:write` when
`source_app` is supplied; verify the current server behavior before claiming
that optional delete grants are enforced by these shared-memory routes.

### Files and object storage

Files are shared platform data, not `apps/files` records. Use Server 1:

- Upload: `POST /api/v1/files` as `multipart/form-data` with a `file` part
  and, when applicable, the initiating app's `source_app`.
- List: `GET /api/v1/files?limit=50&offset=0`.
- Metadata/download: `GET /api/v1/files/{object_id}/metadata` and
  `GET /api/v1/files/{object_id}`.
- Delete: `DELETE /api/v1/files/{object_id}?confirm=true` after explicit
  user confirmation. This also removes the associated document Vault record.

The authenticated user is the owner; never send owner IDs or storage
credentials. Upload checks `files:write` when `source_app` is supplied.
Do not claim that the optional `files:delete` grant is enforced by the
current file-delete route. Honor server-side size/type errors rather than
hard-coding deployment limits. Deleting a Brain Vault record does not delete
the original file binary; use the Files API for that separate operation.

### Other catalog capabilities

Knowledge Graph, Notes, Tasks, Timeline, and Workflow are shared capability
catalog entries, not additional Core apps. A catalog entry does not establish
a complete dedicated API or shipped feature. Do not invent endpoints or
present their record types as implemented Core functionality; consult the
live catalog and backend contracts before adding a client surface.

## 10. Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Core app using this guide as its
API and behavior contract. First inspect the current Android project, product
requirements, build setup, architecture, auth/session flow, API clients, and
tests. Preserve working code and reuse the project's established patterns.
If no app exists, create a native Kotlin Android app using Jetpack Compose
and Material 3; do not build a WebView or a second backend.

Create a polished, interactive, accessible UI with clear navigation for
sign-in/session, Brain chat and status, cited cross-app sources, a complete
user-owned Vault browser/manager organized by source app, authorized search,
shared-memory management, Files, app connections/permissions, and privacy.
Use every applicable, implemented Server 1 capability documented in this
guide; do not invent unsupported server features. Include loading, empty,
offline, retry, and actionable error states. Do not make up data or show
success when an API request failed.

Use only the Server 1 base URL and endpoints in this guide. Integrate account
auth securely; connect `aura-core` by reconciling GET /api/v1/apps/me and
handling POST success/409 correctly. Send Brain queries with a stable
request_id and source_app `aura-core`. Show record sources from structured
response fields. Search and retrieval must honor server authorization.
Include only capabilities whose APIs are documented here; do not invent
Knowledge Graph, Notes, Tasks, Timeline, or Workflow endpoints.

Allow normal Brain queries to use records authorized by active app grants and
show each source. The Vault UI may display all owner namespaces with a clear
source-app filter and must never mislabel ownership. Whole-Vault Mode must be
off by default, explicit and revocable; once enabled, Core can use the Brain
management tools across every app namespace in that user's Brain, including
retained data from disconnected apps. Do not turn it on just because a chat
message requests it. For harness actions, honor the active scope and use only
structured top-level plan fields; require an in-app confirmation for
destructive plans. Keep file-binary deletion separate from Brain-record
deletion.

Never call Server 2 or object storage directly. Never bundle internal tokens,
owner IDs, or secrets, and never log private chat, memory, or file contents.
Implement lifecycle-safe networking, typed API models, and tests for auth,
connection reconciliation, permission denial/revocation, query retries,
Vault scoping, structured confirmations, and account isolation. Run the
available Android unit tests and debug build; fix failures caused by your
changes and report changed files, test/build results, and anything blocked.
```
