# AURA Product and Shared-Capability Inventory

**Canonical product-boundary and application inventory.** This document
describes the requested AURA product architecture separately from Server 1's
implementation catalog. It is based on repository evidence in this checkout;
catalog flags and integration guides do not prove that a user-facing app has
been built or released.

## Evidence and status rules

- **Verified in repository** means a route, model, adapter, or client project
  was found in this working tree.
- **Target product definition** means the role requested for the AURA product;
  it is not a claim that the feature exists.
- **Android/client status** uses the requested evidence categories:
  1. implemented Android application;
  2. partially implemented Android application;
  3. documented but not implemented;
  4. planned application without an implementation;
  5. unable to determine from available evidence.
- Repository search found no Kotlin, Java, Dart, Gradle, or Flutter application
  project. Therefore no Android/client implementation is verified here.
  A missing client in this repository does not prove that no external
  application exists.
- Server 1's `CATALOG` in `server1/app/services/app_registry.py` is
  authoritative for current backend slugs, status flags, and grants. It is
  not the product taxonomy: it currently includes internal capabilities and
  a Chronos marker alongside app entries.

## A. Seven primary AURA applications

The seven products below are the AURA application inventory. Shared platform
capabilities and the independent Chronos product are not additional primary
AURA apps.

### 1. AURA Core

- **Purpose / responsibilities:** Central user-facing AURA entry point for
  account experience, Brain chat/status, authorized cross-app search,
  complete user-authorized Brain/Vault management across apps, settings, and
  privacy.
- **Target major features:** Personal Brain chat; Brain readiness/status;
  authorized retrieval; browsing and organizing app namespaces; user-approved
  organization; app access and privacy settings. This target does not create
  a second Brain or duplicate the vault. Whole-Vault Mode is explicit opt-in;
  ordinary chat is limited to data authorized through active app grants.
- **Verified backend:** Catalog slug `aura-core` (`live`, mobile); Server 1
  supports `POST /api/v1/brain/query`, `POST /api/v1/brain/process`,
  `/api/v1/brain/vault/*`, Brain status, auth, and app-connection routes.
  Query turns are persisted in the caller namespace. Normal queries can use
  cross-app data covered by active `brain:read` grants. Core's Brain harness
  can manage every namespace when the separate whole-vault mode is explicitly
  enabled; ordinary app management remains namespace-scoped.
- **Catalog defaults:** `identity:read`, `brain:read`. Optional grants include
  `memory:write`, `aura-core:delete`, and legacy `records:*`.
- **Client status:** **3 — documented but not implemented in this repository.**
  Core's catalog entry points to `server1/`, which is backend code; no Android
  or web client source/build project was found. External app status is unknown.
- **Dependencies / gaps:** Server 1 auth and gateway; Server 2 per-user Brain
  and Vault. Actual Core UI, Android app, and external deployment cannot be
  verified here.
- **Guides:** [AURA Core guide](./android-app-guides/AURA-CORE-GUIDE.md).

### 2. AURA Daily Diary

- **Purpose / responsibilities:** Capture daily life, personal reflections,
  events, and personal-history context.
- **Target major features:** Written and (where built/consented) voice
  journaling, transcription, event extraction, daily/weekly summaries,
  personal-history search, Diary-owned chronological life timeline, and
  relationships between records.
- **Verified backend:** Catalog slug `diary` (`planned`, mobile); concrete
  `normalize_diary` adapter at `POST /api/v1/apps/diary/ingest`; records are
  stored in the owner's `apps/diary` Vault namespace. Supports entry text,
  mood/date metadata, and idempotent source identifiers.
- **Catalog defaults:** `brain:read`, `memory:write`, `timeline:write`,
  `diary:write`. Additional read/delete and record grants are catalog
  options.
- **Client status:** **4 — planned application without an implementation**
  in this repository. No Android client source/build project was found.
- **Dependencies / gaps:** Shared identity, Brain, Vault, and search.
  Voice, transcription, summaries, and a Diary timeline UI are not verified.
  The adapter normalizes entry content; full raw request preservation is not
  established.
- **Guide:** [Daily Diary guide](./android-app-guides/AURA-DIARY-GUIDE.md).

### 3. AURA Health

- **Purpose / responsibilities:** User-consented health information and
  authorized health insights.
- **Target major features:** Health records/journals, symptoms and
  measurements, medication/appointment tracking where implemented, summaries,
  trends, search, health reminders, and health-specific timeline views.
  These are product goals, not verified features.
- **Verified backend:** Catalog slug `health` (`future`, mobile); a
  `normalize_health` adapter accepts wellness event or locally extracted
  report text and writes under `apps/health`. It is sensitive data; no Health
  client implementation is present in the repository.
- **Catalog defaults:** `brain:read`, `memory:write`,
  `notifications:read`, `health:write`; Health read/delete and record grants
  are optional.
- **Client status:** **4 — planned/future application without an
  implementation** in this repository.
- **Dependencies / gaps:** Shared Brain/Vault/search and explicit Health
  consent. Medication management, reminders, trends, original-binary
  handling, and Health UI are not established.
- **Guide:** [Health guide](./android-app-guides/AURA-HEALTH-GUIDE.md).

### 4. AURA Study Companion

- **Purpose / responsibilities:** Learning, academic organization, and study
  productivity.
- **Target major features:** Materials, subjects/topics, notes and summaries,
  revision plans, learning tasks/deadlines, progress, search, and record
  relationships.
- **Verified backend:** Catalog slug `study` (`planned`, mobile); concrete
  `normalize_study` adapter at `POST /api/v1/apps/study/ingest`; accepts
  notes/transcript plus optional subject/course/duration/recording metadata
  and writes the result to `apps/study`.
- **Catalog defaults:** `brain:read`, `memory:write`, `timeline:write`,
  `study:write`. `study:delete` and record grants are optional.
- **Client status:** **4 — planned application without an implementation**
  in this repository. No Flutter/Android source or build project was found.
- **Dependencies / gaps:** Shared Brain/Vault/search and potentially Timeline.
  Revision planning, progress tracking, client UI, and lossless raw-payload
  retention are not verified.
- **Guide:** [Study guide](./android-app-guides/AURA-STUDY-GUIDE.md).

### 5. AURA Meeting AI

- **Purpose / responsibilities:** Capture and organize meetings and
  conversations, subject to recording consent.
- **Target major features:** Permitted recording/transcription, summaries,
  decisions, action items, meeting notes, participant/project associations,
  follow-up tasks, history/search, and meeting timeline views.
- **Verified backend:** Catalog slug `meeting` (`live`, mobile; `repo_path`
  says `meetingapp (Android)`); `normalize_meeting` adapter at
  `POST /api/v1/apps/meeting/ingest` supports transcript/summary, decisions,
  actions, participants, and event time. No corresponding Android/Gradle
  project exists in this repository, so the external client status is unknown.
- **Catalog defaults:** `brain:read`, `memory:write`, `files:write`,
  `meeting:write`. `meeting:read`, `meeting:delete`, and record grants are
  optional.
- **Client status:** **5 — unable to determine from available evidence.**
  Backend catalog status is `live`, but client source is absent; do not
  interpret that flag as proof of a released Android app.
- **Dependencies / gaps:** Shared Brain/Vault, optional Files, and
  user-authorized recording. A dedicated Task or Reminder store and client
  behavior are not verified.
- **Guide:** [Meeting guide](./android-app-guides/AURA-MEETING-GUIDE.md).

### 6. AURA Travel

- **Purpose / responsibilities:** Travel planning and travel-related
  information.
- **Target major features:** Trips/itineraries, schedules, reservations,
  travel documents, destinations, checklists, budgets, reminders, trip
  timelines, and travel journals/summaries.
- **Verified backend:** Catalog slug `travel` (`planned`, mobile); concrete
  `normalize_travel` adapter at `POST /api/v1/apps/travel/ingest` accepts a
  destination, optional dates/notes, and object reference metadata, then
  stores in `apps/travel`.
- **Catalog defaults:** `brain:read`, `memory:write`, `files:write`,
  `travel:write`. `travel:read`, `travel:delete`, and record grants are
  optional.
- **Client status:** **4 — planned application without an implementation**
  in this repository.
- **Dependencies / gaps:** Shared Brain/Vault, Files, and notification
  capability. Reservations, budgets, checklists, reminders, and Travel UI are
  not verified.
- **Guide:** [Travel guide](./android-app-guides/AURA-TRAVEL-GUIDE.md).

### 7. AURA Notification Hub

- **Purpose / responsibilities:** A separate first-class AURA application for
  notifications, reminders, scheduling, and cross-app event overview.
- **Target major features:** Notification center/history, scheduled and
  user-created reminders, upcoming events, snooze/dismissal, domain reminders,
  links to source records/apps, unified event timeline, preferences, and
  AI-assisted notification management where supported.
- **Authoritative data boundary:** Own notification/reminder definitions,
  schedule, delivery status, read/dismissed/snoozed state, preferences, and
  references to source records. It must not duplicate whole Diary, Health,
  Meeting, Study, or Travel records.
- **Verified backend:** Catalog slug `notifications` (`planned`, `internal`);
  `normalize_notification` adapter at
  `POST /api/v1/apps/notifications/ingest` stores an activity-only record
  (no Brain memory) in `apps/notifications`. No push delivery, reminder
  scheduler, notification preference API, or separate mobile client was found.
- **Catalog defaults:** `brain:read`, `memory:write`,
  `notifications:read`, `notifications:write`; `notifications:delete` and
  record grants are optional. Existing legacy connections do not gain new
  default grants automatically.
- **Client status:** **4 — planned application without an implementation**
  in this repository. Catalog still calls the entry `internal`; aligning the
  catalog with the requested first-class mobile product is a product/backend
  decision, not implemented here.
- **Dependencies / gaps:** Shared identity, Brain/Vault, event references,
  scheduling and delivery services. Current ingestion is not a reminder
  system and does not establish authoritative reminder CRUD.
- **Guide:** [Notifications guide](./android-app-guides/AURA-NOTIFICATIONS-GUIDE.md).

## B. Shared AURA capabilities

| Capability | Type and purpose | Current repository evidence | Target owner / consumers | Gaps |
|---|---|---|---|---|
| Personal Brain | Per-user inference/runtime and controlled tools; shared service, not an Android app | Server 1 query/process gateway; Server 2 Brain provisioning, runtime, analyze/infer, and harness code | Shared by all seven apps under user grants; Core is primary direct UI | No app clients in tree; provider mode can share external provider weights; runtime availability depends on deployment |
| Personal Vault | Authoritative per-Brain record store with app namespaces | Server 2 per-Brain `memory_vault/vault.sqlite`; Server 1 `/api/v1/brain/vault/*`; `apps/{slug}`, `shared`, `conversations`, `documents` namespaces | Shared platform data; each app manages its own namespace; Core may manage cross-app after opt-in | App ingestion normalizes payloads; lossless original payload preservation is not established for all adapters; at-rest encryption is not implemented by this application |
| Memory | Curated/retrieved personal information, distinct from all raw records | `/api/v1/brain/memory*` routes operate on Brain Vault records; query retrieval is separate from complete vault storage | Brain capability used by authorized apps; Core/Memory interfaces consume it | No separate memory-filtering feature should be assumed; older docs describe Postgres memory behavior that no longer matches all current code |
| Search | Authorized discovery over records | `/api/v1/brain/vault/search`, plus Brain query retrieval; Server 1 computes namespaces using active grants | Shared by Core and other authorized apps | Search UI is not present; complete indexed/semantic behavior varies by runtime/storage configuration |
| Knowledge Graph | Relationships among records/entities | Server 1 `KnowledgeGraph` ORM model/repository and catalog `knowledge-graph`; analysis metadata includes entities/topics | Shared capability for Core, Diary, Study, Meeting, Travel | No standalone graph client/product or dedicated public CRUD API verified; data writer coverage is limited/unclear |
| Notes | Note-taking feature, not necessarily a product | `notes` catalog entry and generic Vault record endpoint; no `notes` ingestion adapter | Owned within Diary, Study, Meeting, Core, and other domain apps as appropriate | Catalog still lists planned `notes` as a mobile app; no product decision or adapter verified |
| Files | Shared binary/object and document-text storage | Server 1 `/api/v1/files*` proxies to Server 2 storage; Files catalog entry; document namespace | Used by apps that upload/attach files; not a primary app in target inventory | Catalog has an internal `files` entry; document extraction and original-file/vault-record lifecycle depend on configured storage |
| Tasks | Cross-domain task/action-item capability | `tasks` catalog entry and generic Vault record support; no task-specific adapter or dedicated task API/model verified | Academic tasks in Study, meeting follow-ups in Meeting, travel prep in Travel, personal tasks in Diary; Notification Hub schedules reminders | Authoritative task ownership and CRUD contract unresolved; do not create a separate Tasks app as part of this audit |
| Workflow | Orchestration capability | `workflow` catalog entry; no dedicated workflow runtime/API found in inspected app routes | Brain harness and authorized platform operations | Catalog status is planned; no user-facing app or concrete workflow subsystem verified |
| Timeline | Dated-event capability, not a standalone app | Server 1 Timeline ORM/repository; PersonalBrain service has timeline operations; separate catalog entry and no app-specific ingestion adapter | Diary owns personal-life chronology; Notification Hub owns unified upcoming/cross-app event view; Core may show broad overview | Not all current event ingestion is wired to Timeline; no standalone Timeline app is part of target architecture |
| Scheduling / time | Shared scheduling utilities | No AURA-specific reminder scheduler or dedicated public scheduling API was verified | Notification Hub is the target user-facing owner; domain apps may create their own events | No concrete AURA scheduling service found. Do not rename code as part of this task |
| Notification delivery/history | Reminder scheduling and delivery state | Notifications ingestion adapter accepts notification events only | Notification Hub owns its notification/reminder state; source apps own their domain records | Push/subscription delivery, scheduling, read state, and reminder CRUD are not implemented/verified |

### Data preservation status

The current backend does not prove the strict target that every accepted
request is preserved losslessly in its original form. App adapters normalize
source payloads into content and selected metadata; the ingestion route
removes the adapter's `raw` metadata before writing the Vault record.
Universal `/api/v1/brain/process` stores analyzed segments under the source
namespace, rather than retaining a byte-for-byte copy of every source
payload. Record processing, indexing, and object references exist, but
schema-versioned raw-payload retention and per-field source/receipt/update
timestamps are not universal. Treat lossless preservation as an unresolved
architecture gap, not as an implemented guarantee. No migration, storage, or
backend changes are made by this inventory task.

## C. Independent product: Chronos

**Chronos is a separate enterprise product, not an AURA application.** It is
not AURA Core, Notification Hub, Timeline, or an AURA scheduling module and
must not be counted as an eighth AURA app.

Repository evidence currently shows only AURA-side integration markers:

- An `AppDefinition` with slug `chronos`, `platform_type="chronos"`, and a
  description calling it an AURA platform-family marker.
- A `PlatformType.CHRONOS` enum value.
- Historical `chronos_knowledge` collection references.
- No Chronos-owned application, enterprise product code, UI, or deployment
  was found.
- The generic AURA app connection and Brain-query gateway can technically
  authorize a `chronos` source slug. That is an AURA integration surface, not
  evidence that the separate Chronos product is integrated or deployed.

Any Chronos use of AURA Brain/Vault or other services must be an explicit,
authorized, tenant-isolated external integration. Its existence, identity
model, lifecycle, and operational status remain **unverified**. The
[Chronos guide](./android-app-guides/AURA-CHRONOS-GUIDE.md) retains technical
catalog details but should be read as an AURA-side compatibility note, not an
AURA app guide. Rename decisions are out of scope.

## D. Existing documentation and guide mapping

| Document | Actual subject / owner | Relationship to this inventory | Required interpretation |
|---|---|---|---|
| `docs/AURA_APP_INVENTORY.md` | Product boundaries, seven apps, shared capabilities, Chronos | **Canonical source of truth** | Use it for product classification; use the runtime catalog only for implementation facts |
| `docs/AURA_APP_INTEGRATIONS.md` | Server-side ingestion and API implementation | Supporting backend report | Do not use its old app list as product taxonomy; keep API claims aligned with code |
| `docs/AURA_BRAIN_PROCESSING.md` | Universal process/analyze pipeline | Shared Brain technical contract | Pipeline exists; cross-app delivery and review remain unimplemented as documented |
| `docs/AURA_IMPLEMENTATION_AUDIT.md` | Earlier implementation audit | Historical evidence | Its count/status summaries are stale; it does not override this inventory |
| `docs/AURA_RENDER_SETUP.md` | Deployment/runtime setup | Shared platform operations | Not an app inventory |
| `docs/android-app-guides/README.md` | Android Studio guide index | Navigation only | One guide per primary AURA app plus a separate Chronos guide |
| `AURA-CORE-GUIDE.md` | AURA Core client/API contract | Primary app technical guide | Backend contract is usable; client remains absent from this repository |
| `AURA-DIARY-GUIDE.md` | Daily Diary adapter and Vault integration | Primary app technical guide | Preserve; UI/features beyond adapter are not verified |
| `AURA-HEALTH-GUIDE.md` | Health consent, adapter, Vault | Primary app technical guide | Preserve; catalog is `future`, not released |
| `AURA-STUDY-GUIDE.md` | Study adapter and Vault | Primary app technical guide | Preserve; catalog is `planned`, not released |
| `AURA-MEETING-GUIDE.md` | Meeting adapter and Vault | Primary app technical guide | Preserve; catalog `live` is not proof of an in-repository client |
| `AURA-TRAVEL-GUIDE.md` | Travel adapter and Vault | Primary app technical guide | Preserve; catalog is `planned`, not released |
| `AURA-NOTIFICATIONS-GUIDE.md` | Notification event adapter | Primary app technical guide (target product is Notification Hub) | Preserve; guide must not imply reminder scheduling/delivery exists |
| `AURA-CHRONOS-GUIDE.md` | AURA-side Chronos catalog entry | Independent-product compatibility note | Do not count as an AURA app; actual Chronos integration is unverified |

Shared capabilities are documented within the relevant app handoff where
needed; they do not have separate Android Studio guides. The inventory and
backend integration report remain supporting references, not extra app guides.

## E. Implementation status and backend boundaries

### Client/application status

No Android, Flutter, Dart, Java, Kotlin, Gradle, or web application source
project was discovered in the repository. All dedicated guides are
documentation, not evidence of implemented clients. The Meeting catalog's
`repo_path="meetingapp (Android)"` is a string in the registry; the referenced
project was not found in this tree. Whether any client is implemented or
deployed outside this repository is unknown.

### Server 1

Server 1 is the public authenticated gateway. Verified routes include:

- Auth and user identity under `/api/v1/auth` and `/api/v1/users`.
- App catalog/connections and concrete ingestion at
  `/api/v1/apps`, `/api/v1/apps/me`, and `/api/v1/apps/{slug}/ingest`.
- Brain query, ingest, memory operations, status, and processing under
  `/api/v1/brain/*`.
- Vault record/search gateway at `/api/v1/brain/vault/*`.
- Files gateway at `/api/v1/files*`.

It authenticates users, enforces per-app permissions, resolves the owner's
Brain, and forwards authorized operations to Server 2.

### Server 2

Server 2 hosts the per-user Brain lifecycle/runtime, controlled harness,
per-Brain vault, and object-storage APIs. The repository's Server 2 guide
describes a per-Brain SQLite vault and per-Brain private model copies in
local dedicated mode. External-provider mode necessarily uses provider-hosted
shared model weights; model-copy isolation is therefore mode-dependent.
Storage and model implementation details are in
`server2/README.md`; they do not imply an Android app exists.

### Integration path

```text
AURA client (not present in this repository)
  -> Server 1 (auth, app grants, public gateway)
  -> authenticated user's Brain on Server 2
       -> per-Brain Vault namespaces: apps/{slug}, shared,
          conversations, documents
       -> inference / controlled harness / object storage
```

Normal retrieval uses active, granted app namespaces. Harness requests are
app-local by default; only AURA Core's explicit whole-vault mode is intended
to permit cross-app management. All user data is expected to stay scoped to
the authenticated owner and the canonical Brain.

## F. Product boundaries and unresolved decisions

### Verified facts

- The registry currently has 17 entries; that is not the number of primary
  AURA apps.
- Concrete app ingestion adapters exist for `brain`, `study`, `diary`,
  `meeting`, `travel`, `health`, and `notifications`.
- `notes`, `tasks`, `timeline`, `workflow`, `search`, `knowledge-graph`,
  `chronos`, `aura-core`, `memory`, and `files` do not have concrete
  app-specific adapters in the current adapter registry.
- Notification ingestion stores an activity record only; it does not
  implement scheduling or delivery.
- No stand-alone Timeline app, Tasks app, Files app, or Notes app is part of
  the target seven-app inventory.

### Product decisions still needed

1. Align the catalog's `notifications` entry (`internal`, `planned`) with the
   intended first-class mobile Notification Hub, or explicitly retain an
   internal adapter and add a distinct product slug.
2. Decide whether to keep compatibility catalog entries for `notes`, `tasks`,
   `timeline`, `search`, `knowledge-graph`, and `workflow` while documenting
   them only as capabilities.
3. Decide the authoritative task/reminder schema and which app owns each
   record versus its schedule/reference.
4. Decide how Diary timeline, Notification Hub cross-app timeline, and Core's
   broad overview share event references without storing duplicate records.
5. Define lossless raw payload retention, event/source/received/update
   timestamps, schema versioning, integrity hashes, recovery/version policy,
   backup, and at-rest encryption requirements.
6. Confirm whether the external Chronos product uses AURA services. No such
   integration is proven by the catalog marker.

### Recommended implementation priorities (not implemented here)

1. Approve the seven-product boundary and settle Notification Hub's catalog
   identity.
2. Inventory external Android/client repositories and link their actual
   source/build locations to these guides.
3. Specify Notification Hub reminder/schedule/delivery data ownership and
   APIs without copying domain records.
4. Specify and test complete original-payload retention and recovery before
   expanding app ingestion.
5. Verify access, isolation, lifecycle, and deployment contracts for any
   external Chronos integration.
