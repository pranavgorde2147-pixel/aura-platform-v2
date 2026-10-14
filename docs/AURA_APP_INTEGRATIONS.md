# AURA Application Integrations (Phase I)

Status of the planned AURA applications and the concrete server-side ingestion
integration implemented for each. **No frontend client code exists in this
repository** — there is no Flutter/Dart, Android, or web project anywhere in the
tree. Everything below is the Server 1 backend integration; the mobile/web
clients are still to be built.

Shared prerequisites already implemented and reused by every app:

- **Shared AURA identity and authentication** — accounts, login, access/refresh
  tokens, sessions and app authorization all live in Server 1
  (`/api/v1/auth`, `/api/v1/users`, `/api/v1/apps`). Apps authenticate as the
  user; the backend resolves the owner from the bearer token, never from the
  payload.
- **Per-user app connections** — `InstalledModule` + `ModulePermission`
  (`/api/v1/apps/me`). A user must connect an app before it can send anything.
- **Ingestion endpoint** — `POST /api/v1/apps/{slug}/ingest`. The authenticated
  user is resolved server-side; the connection's active permission for the app's
  declared scope/action is enforced before any write.
- **Brain write path** — each event writes a brain memory (unless the event is
  activity-only), a `memory_vault` pointer when an object is referenced, a
  `knowledge_graph` node increment, and a dated `timeline` event carrying
  `source_app` provenance. Eligible text is embedded and indexed for retrieval
  (graceful keyword fallback when Qdrant is unavailable).
- **Idempotency** — supplying `external_id` (or `event_id`/`id`) makes the write
  idempotent: a replay with the same external id is detected via the timeline and
  does not create a second memory.
- **Partial-failure safety** — the memory is committed first; if a vault /
  knowledge / timeline side effect fails, the memory is preserved and the
  response reports `partial: true` instead of losing data.

## Per-application status

### 1. AURA Chat — central personal AI assistant

- Frontend: **missing**.
- Backend: **partial / live** — the primary surface is the Personal AI Brain
  (`POST /api/v1/brain/query`, `POST /api/v1/brain/ingest`) under the `brain`
  catalog entry. Persisted chat turns are supported through the `brain` adapter
  (`chat.message`, interpretation). Conversations are never saved automatically.
- Remaining: a chat client and an explicit "save this turn" UX.

### 2. Study Companion — Flutter, Theme B (calm, academic)

- Frontend: **missing** (spec says Flutter, Theme B; no Flutter project exists).
- Backend: **implemented** — catalog `study` (planned) + `normalize_study` adapter
  (`study.lecture_recorded`). Accepts notes/transcript/title/subject and an
  optional attached document; stores a fact memory, timeline event, knowledge
  node and vault pointer.
- Remaining: the Flutter client and Theme B design system.

### 3. Daily Diary

- Frontend: **missing**.
- Backend: **implemented** — new catalog `diary` (planned) + `normalize_diary`
  adapter (`diary.entry`). Entries are stored as interpretation memories (the
  user's own reflection) with timeline provenance; optional mood/date.
- Remaining: the diary client.

### 4. Meeting Assistant

- Frontend: **missing** (catalog references `meetingapp (Android)`; no Android
  project exists in this repo).
- Backend: **implemented** — catalog `meeting` (live) + `normalize_meeting`
  adapter (`meeting.recorded`). Accepts transcript/summary, decisions, action
  items, participants and an optional transcript file; stores a fact memory,
  timeline event, knowledge node and vault pointer.
- Remaining: the Android client; routing decisions/action items into dedicated
  Task/Notification stores (no such models exist yet — currently preserved in
  the timeline event metadata).

### 5. Travel App

- Frontend: **missing**.
- Backend: **implemented** — new catalog `travel` (planned) + `normalize_travel`
  adapter (`travel.trip`). Accepts destination, dates, notes and an optional
  attachment; stores a fact memory, timeline event, knowledge node and vault
  pointer.
- Remaining: the travel client.

### 6. Notification Hub

- Frontend: **missing** (internal service).
- Backend: **implemented** — catalog `notifications` (planned, now with
  `notifications:write`) + `normalize_notification` adapter
  (`notification.received`). Notification events are activity-only: they write a
  timeline event and knowledge node but deliberately create **no** brain memory.
- Remaining: an actual delivery/push channel and a client subscription surface.

### 7. Shared AURA identity and authentication

- Frontend: **missing** (no shared client SDK).
- Backend: **implemented** — registration, login, token refresh with rotation +
  reuse detection, logout/revocation, `/auth/me`, and app authorization. This is
  the same identity every app above authenticates against.
- Remaining: a shared client SDK / login UI for the Flutter/web clients.

## Extension point

Add an app by defining an `AppAdapter` in
`server1/app/services/app_ingestion.py` (`slug`, `scope`, `action`, `normalize`)
and ensuring a catalog entry grants that scope/action by default. Add a
normalizer for the app's documented `sends_to_aura` payload; do not invent fields
beyond the spec.
