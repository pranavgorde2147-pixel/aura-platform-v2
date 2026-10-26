# AURA Study App – Android Studio Integration Guide

**App Slug:** `study`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions (granted on connect):** `brain:read`, `memory:write`, `timeline:write`, `study:write`
**Optional Permissions:** `records:read`, `records:write`, `records:delete`,
`study:read`, `study:delete`
**Ingestion Adapter:** `/api/v1/apps/study/ingest` → `normalize_study` (stored in the user's Server 2 Brain vault as `apps/study/`)

App ingestion, processing results, and related document references are stored
in Server 2, not in Server 1's Render PostgreSQL content tables. The app must
keep local unsynchronized records until Server 1 confirms successful storage.
Use `https://aura-server1a-core.onrender.com/` as the only API origin and send
the logged-in user's bearer token. Never call Server 2 directly.

## 1) Endpoints
| Action | Method | Path | Auth |
| --- | --- | --- | --- |
| Catalog (verify app exists) | GET | `/api/v1/apps/study` | No |
| Connect app | POST | `/api/v1/apps/me` | Bearer token |
| List my connections | GET | `/api/v1/apps/me` | Bearer token |
| Ingest lecture/study record | POST | `/api/v1/apps/study/ingest` | Bearer token (connection required) |
| Query brain (optional) | POST | `/api/v1/brain/query` | Bearer token (connection with `brain:read` required if `source_app="study"`) |

## 2) Configuration
Create `app/src/main/res/xml/network_security_config.xml` or use HTTPS. Store base URL in `BuildConfig` or `local.properties`:

```kotlin
object AuraConfig {
  const val BASE_URL = "https://aura-server1a-core.onrender.com"
}
```

## 3) Auto-connect flow (implement this)
Do this **after successful login** (when you have `access_token`):

```kotlin
// 1) Ensure app exists in catalog (optional but good for validation)
GET $BASE_URL/api/v1/apps/study

// 2) Try to connect
POST $BASE_URL/api/v1/apps/me
Headers: Authorization: Bearer $accessToken, Content-Type: application/json
Body: {"module_slug": "study"}

// 3) Handle response
201 Created -> connected (save connection_id + permissions in prefs/Room)
409 Conflict -> already connected (fetch /apps/me and cache)
404 Not Found -> app not in catalog (log error, do not retry)
```

Cache `is_active`, `permissions[]` (must include `brain:read` for queries).

## 4) Ingest data (store lecture notes)
**Preferred endpoint:** `POST /api/v1/apps/study/ingest`

```json
{
  "notes": "Lecture 3 - Normalization (1NF, 2NF, 3NF). Example: Student(Course, Instructor) split...",
  "title": "DBMS Normalization",
  "subject": "DBMS",
  "course": "CS301",
  "duration_minutes": 45,
  "external_id": "study-lecture-001",  // used for deduplication
  "object_reference": "objects/lecture3.pdf"  // optional, stored in vault
}
```

Adapter expects at least one of `notes|transcript|content|text|summary`. On
success (201), `record_id` identifies the Server 2 vault record. Always provide
a stable `external_id` and reuse it for sync retries; a conflicting reuse
returns `409`.
Optional keys include `title`/`topic`/`subject`, `course`, `duration_minutes`,
`recorded_at`, `object_reference`, `object_key`, `file_id`, `document_id`,
`attachment`, `storage_type`, `mime_type`, `file_size_bytes`, and `checksum`.
The adapter reads dedupe identifiers from `external_id`, `event_id`,
`document_id`, `id`, `uuid`, or `reference`. Ingestion response keys are
`accepted`, `source_app`, `event_type`, `duplicate`, `partial`, `memory_id`,
`vault_entry_id`, and `record_id`.

List this app's records with `/api/v1/brain/vault/records?source_app=study`.
Search with `POST /api/v1/brain/vault/search` using
`{"source_app":"study","query":"..."}`. Results include other active app
namespaces, conversation turns and authorized documents only when the owner
has granted those app connections `brain:read` (and Files `files:read` for
document content). Keep the Study Vault screen restricted to `apps/study`;
search results may cite other authorized apps, but do not add cross-app
record-list controls to the Study Vault.

## 5) Query brain (optional)
```json
POST /api/v1/brain/query
Headers: Authorization: Bearer $accessToken
{
  "request_id": "<stable UUID for retries>",
  "query": "Explain 3NF with example",
  "context": {},
  "source_app": "study",
  "use_memory": true,
  "memory_limit": 5
}
```
Expect 200 with `answer`, `source`, and `request_id`. Query and answer are
persisted in the Brain's `apps/study` namespace and can be retrieved by any
connected app with the owner's `brain:read` grant. Reuse the same
`request_id` on a retry. Query persistence is automatic, including for legacy
clients that send `persist_interaction:false`. If inference fails, the query
itself remains in the private vault and a retry with the same `request_id`
completes that saved turn. If 403, inspect the app connection permissions; if
503, preserve local state and retry after Server 2 is reachable.

With `use_memory:true`, Study queries and Brain search can include read-only
context from other active app connections with `brain:read`; document content
also requires Files `files:read`. Show source attribution. Cross-app context
is not part of the Study Vault and cannot be edited or deleted from Study.

## 6) Edit or delete Study vault records

Study's default `memory:write` grant allows the user to edit records in the
Study app's own `apps/study` namespace. Load the current record and version
before presenting an edit form. Updates use optimistic concurrency; changing
original content requires explicit confirmation:

```http
PUT /api/v1/brain/vault/records/{record_id}?source_app=study&client_app=study
Authorization: ******
Content-Type: application/json

{"expected_version":2,"content":"User-approved update","confirm_original_edit":true}
```

Deletion is a separate soft-delete operation. It is not included in Study's
default grants: explain the impact, request the additional `study:delete`
permission, and retain all existing permissions when updating the connection
(a supplied permission list replaces the full set). Then require a clear user
confirmation and call:

```http
DELETE /api/v1/brain/vault/records/{record_id}?source_app=study&client_app=study&confirm=true
Authorization: ******
```

Through the direct Vault gateway, AURA Core edits require Core `memory:write`
and an active Study connection with `brain:read`; direct deletes also require
Study's `study:delete` grant and explicit confirmation. Core's separate
Whole-Vault Mode can manage Study records through Brain tools after the owner
explicitly enables it and confirms each destructive plan. All operations
remain scoped to the signed-in owner's Brain.

AI-managed edits replace current content without retaining prior versions.
For AI deletion, Core's Brain-management tool has a separate confirmation
flow: show the previewed app namespace and exact record count, then bind
confirmation to that namespace/count. AI deletion permanently purges Study
vault records and Brain-side versions/indexes; it does not delete original
files stored through Files.

## 7) Study-local harness

For AI-assisted Study changes, send the user's message to
`POST /api/v1/brain/query` with `source_app:"study"` and the literal
`harness:` prefix and `use_memory:true`. The request can use authorized
cross-app records as read-only context, while harness tools remain restricted
to `apps/study`. Edits and creates may apply immediately. Delete and
bulk-replacement tools return structured plans; open the confirmation UI only
from top-level plan fields, and submit the selected plan ID with the same
source app and a new request ID after the user confirms.

The Study Vault screen should list only
`GET /api/v1/brain/vault/records?source_app=study&limit=50&offset=0`, paginate
results, and let the user manually edit/delete Study records using the
endpoints above. Do not add cross-app browsing to this screen.

## 7) Verification checklist
- [ ] Login succeeds, token stored securely
- [ ] `GET /api/v1/apps/study` returns `queries_brain: true` and `default_permissions` includes `brain:read`
- [ ] Auto-connect called once; 201 or 409 handled
- [ ] `GET /api/v1/apps/me` shows `study` active with `brain:read`,`memory:write`,`timeline:write`
- [ ] Ingest returns 201 with a Server 2 `record_id`
- [ ] Brain query returns 200 (no 403)
- [ ] User isolation verified (another user can't see this data)

## 8) Notes for agent
- Never call `ai_server_url` directly; always go through Server 1.
- Use `external_id` for dedup on repeat syncs.
- Never bundle the Server 2 internal token in Android.
- Treat 409 on connect as success.
- Don't log tokens/PII in crash reports.

## 9) Copy/paste Android Studio implementation prompt

```text
Build or complete the native Android AURA Study Companion using this guide
as its API and behavior contract. Inspect the project, requirements,
architecture, auth, networking, local persistence, and tests first. Reuse
the app's existing patterns; if starting from an empty project, use native
Kotlin, Jetpack Compose, and Material 3, not a WebView or separate backend.

Create a focused, motivating study workspace with subject/course organization,
lecture-note capture, a study-record list, Brain Q&A, and a Study-only Vault.
Use clear hierarchy and accessible typography, with responsive empty/loading/
offline/sync/error states. Features such as quizzes or study plans may be
local UI only unless this project already has a real implementation/API;
never pretend unsupported features were saved to the server.

Use only the Server 1 endpoints and `study` payload fields in this guide.
Brain Q&A/search should use authorized cross-app records as read-only context
when active connections grant `brain:read` (and Files `files:read` for
documents), while showing source attribution. Connect through
/api/v1/apps/me and reconcile 409. Persist unsynced notes
locally, assign a stable external_id, and reuse it with the same content on
retry. Brain queries must use source_app `study`. Keep the Study Vault screen and all
edits/deletes inside `apps/study`. Harness may use authorized cross-app
records as read-only context, but its tools may manage only Study records.
Use expected_version and explicit confirmations. For harness delete or bulk
replacement, read structured plans and show native confirmation before
approval.

Never call Server 2 directly or include internal credentials in the app.
Avoid logging notes, tokens, or personal data. Add tests for connect/reconcile,
offline idempotent sync, query permissions, Vault scoping, version conflicts,
and destructive confirmations. Run available Android tests and debug build;
fix regressions and report changes and results.
```
