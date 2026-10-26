# AURA Server-Side App Integration Status

This is the backend implementation companion to the canonical
[AURA Product and Shared-Capability Inventory](./AURA_APP_INVENTORY.md).
It records routes and adapters verified in this repository; it is not the
product taxonomy. The registry includes primary app entries, shared
capabilities, and a legacy Chronos integration marker.

## Architecture boundary

- **Server 1** (`server1/`) provides the public auth, app-connection,
  permission, Brain-query, processing, Vault, and file gateway APIs.
- **Server 2** (`server2/`) hosts the authenticated per-user Brain runtime,
  harness/tool execution, per-Brain Vault, and object storage APIs.
- Clients use Server 1 only. Server 1 derives the owner from the user's auth
  token and forwards authorized operations to the owner's Brain.
- **No Android, Flutter/Dart, Java/Kotlin, Gradle, or web client project was
  found in this repository.** A catalog entry or Markdown guide is not proof
  of a released application.

The seven primary AURA products and capability boundaries are defined in the
canonical inventory. Do not treat internal catalog capabilities as additional
primary applications, and do not treat Chronos as an AURA product.

## Common API contracts

### App catalog and user connections

- `GET /api/v1/apps` and `GET /api/v1/apps/{slug}` are public catalog reads.
- `GET /api/v1/apps/me` lists the authenticated user's connections.
- `POST /api/v1/apps/me` connects with `{"module_slug":"<catalog-slug>"}`;
  catalog defaults apply when `permissions` is omitted.
- `PATCH /api/v1/apps/me/{connection_id}` updates status or permissions.
  A provided permissions array **replaces the complete grant list**.
- `DELETE /api/v1/apps/me/{connection_id}` revokes a connection.
- `POST /api/v1/apps/{slug}/ingest` requires an active connection and the
  adapter's declared permission. The owner is taken from authentication, not
  the request body.

### Brain query, processing, Vault, and files

- `POST /api/v1/brain/query` handles natural-language queries and persists
  query/answer turns in the caller's namespace. `source_app` requires its
  active `brain:read` grant. With `use_memory:true`, retrieval includes
  shared data and all active app namespaces whose connections grant
  `brain:read`; document content additionally requires Files `files:read`.
  For a `harness:` request, these other namespaces provide read-only context,
  while the harness tools remain limited to the caller app's namespace.
  The prefix enables fixed search/list/get/find-duplicates/create/update/
  delete-plan tools; duplicate matching is exact after case and whitespace
  normalization and scans at most 1,000 authorized records. It does not grant
  app permissions or Whole-Vault Mode. Writes and app-local delete plans
  require the caller's `memory:write` grant. Delete and exact text-replacement
  plans require a separate approval request. A replacement request naming one
  app is restricted to that namespace; Whole-Vault replacement requires Core's
  explicit Whole-Vault Mode. Literal text replacement, app-namespace deletion
  plans, and plan approvals are attempted through a deterministic Server 2
  Harness path before model inference; other requests use the normal Brain path.
- `POST /api/v1/brain/process` analyzes input and stores derived segments in
  the source app namespace. Requested non-source destinations are reported
  blocked; cross-app delivery/review actions are not implemented.
- `/api/v1/brain/vault/records*` and `/api/v1/brain/vault/search` expose
  authenticated owner-scoped Vault operations with app permission checks.
- Each app's Vault UI should list and manage only its own namespace. Other
  apps' records surfaced through Brain query/search are read-only context,
  with source attribution; do not present them as records owned by the caller.
- AURA Core's separately enabled `manage_vault:true` mode is the exception:
  its Brain management tools can access every namespace in that user's Brain.
  The app must keep this off by default and require explicit user opt-in and
  confirmation for destructive operations.
- `/api/v1/files*` proxies file operations to Server 2 object storage.
- Server 2 internal endpoints and credentials are not client APIs.

For full product/capability ownership, route status, grants, and integration
gaps, see the canonical inventory. For the processing request/response
contract, see [AURA Brain Processing](./AURA_BRAIN_PROCESSING.md).

## Concrete app ingestion adapters

The current adapter registry is
`server1/app/services/app_ingestion.py`; Server 1's route is
`server1/app/api/v1/apps.py`.

| Catalog slug | Adapter | Required input | Normalized event / record | Adapter permission |
|---|---|---|---|---|
| `brain` | `normalize_chat` | `message`, `content`, `text`, or `role_content` | `chat.message` / `chat_message` | `memory:write` |
| `study` | `normalize_study` | `notes`, `transcript`, `content`, `text`, or `summary` | `study.lecture_recorded` / `study_record` | `memory:write` |
| `diary` | `normalize_diary` | `entry`, `text`, `content`, or `body` | `diary.entry` / `diary_entry` | `memory:write` |
| `meeting` | `normalize_meeting` | `transcript`, `summary`, `notes`, `content`, or `text` | `meeting.recorded` / `meeting` | `memory:write` |
| `travel` | `normalize_travel` | `destination`, `destination_name`, `place`, or `title` | `travel.trip` / `trip` | `memory:write` |
| `health` | `normalize_health` | wellness text/value or extracted report text | `health.wellness_event` or `health.medical_report` | `memory:write` |
| `notifications` | `normalize_notification` | `message`, `body`, `text`, `content`, or `title` | `notification.received` / `notification` | `notifications:write` |

Each app's single Android Studio guide documents optional payload keys and
exact request examples. Adapters may normalize and retain selected metadata.
The app route
removes the adapter's `raw` metadata before persisting its Vault record; do
not claim that the full original JSON payload is retained losslessly.

For each accepted adapter input, Server 1 writes one record in
`apps/{source_app}` using an idempotency key derived from the adapter's
dedupe identifier or normalized request. The response includes
`accepted`, `source_app`, `event_type`, `duplicate`, `partial`, `memory_id`,
`vault_entry_id`, and `record_id`. In this route, IDs named `memory_id` or
`vault_entry_id` may refer to the same Vault record; they do not establish a
separate Postgres memory write. Notifications intentionally return
`memory_id:null`.

### Catalog entries without an app-specific ingestion adapter

`aura-core`, `chronos`, `files`, `knowledge-graph`, `memory`, `notes`,
`search`, `tasks`, `timeline`, and `workflow` have no adapter entry in
`app_ingestion.py`. The universal Brain process API and generic Vault gateway
are separate contracts; their existence does not create an app-specific
ingestion adapter or a client.

## Universal Brain processing pipeline

`POST /api/v1/brain/process` is implemented in
`server1/app/api/v1/processing.py`. It authenticates the user, validates the
source catalog entry and permission, asks Server 2 to analyze content, and
stores returned segments under `apps/{source_app}` in the authenticated
owner's Vault. The response includes item classifications, destinations,
authorization and delivery outcomes.

The current code marks requested destinations other than the source as
blocked; it does not deliver records across apps. Processing confirm/retry/
cancel routes exist but currently return `410 Gone`, as described in
[AURA Brain Processing](./AURA_BRAIN_PROCESSING.md). The endpoint stores
analyzed segments, not a guaranteed byte-for-byte raw request. Do not
describe complete source-data preservation or cross-app routing as
implemented.

## Backend limitations to keep visible

- App-specific Android/web clients are absent from this repository; external
  client existence is unknown.
- Ingestion adapters normalize data and do not universally preserve the raw
  submitted payload.
- Notification ingestion is an event-record adapter, not reminder scheduling,
  push delivery, read/dismiss state, or a Notification Hub backend.
- Harness does not provide dedicated move, merge, duplicate-cleanup, or
  reminder-scheduling operations. Duplicate detection is read-only; it does
  not automatically delete duplicate records. Do not represent unsupported
  operations as server-backed features. Title and metadata changes can use
  `vault.update_record`, and copying can create a derived record only within
  the caller's authorized namespace. AURA Core's explicit Whole-Vault Mode is
  needed for cross-app mutations.
- Notes, Tasks, Timeline, Workflow, Search, and Knowledge Graph catalog
  entries do not by themselves establish complete feature APIs or products.
- Chronos catalog/enum/collection references are AURA-side compatibility
  markers only; the independent Chronos enterprise product and any live
  integration are not verified.
- Harness app scope is enforced independently of the `harness:` prefix;
  permission checks and confirmation plans remain active.
