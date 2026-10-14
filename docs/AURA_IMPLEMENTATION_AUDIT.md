# AURA Platform — Implementation Audit

Status: Phase A deliverable (audit). Evidence is drawn from the repository at the
current working tree, not from intent. Each subsystem is classified as
**implemented / under-tested / partial / mock / missing / blocked**.

Scope: two servers only — Server 1 (AURA Core, `server1/`, port 8000, public) and
Server 2 (AURA Brain + object storage, `server2/`, port 8001, internal).

> Secrets: this document contains no secret values. Rotate any credential that has
> ever been shared in plaintext (a live Qdrant key was pasted into a prior session
> and must be treated as compromised).

---

## 1. Executive summary

The repository is a coherent two-server skeleton with real Postgres persistence,
real auth (password + JWT), a working app-registry/permission layer, real file
storage (Server 2), and a real Server-2 inference boundary. The **Personal Brain
is only half real**: memories persist to Postgres and are retrieved by SQL
`ILIKE`, but there is **no embedding model and no vector write/read path** —
Qdrant is connected and health-checked but never used to store or retrieve
memory. Sessions/devices exist as migrated tables but are never written; logout
is a no-op and refresh tokens never rotate. The `memory_vault` and
`knowledge_graph` tables have repositories but no writers. Several Server 2
modules are dead code.

Highest-risk gaps for the stated product goal are: (a) semantic memory not
implemented, (b) session/refresh lifecycle missing, (c) dead code and mock
providers that could be mistaken for real behavior.

---

## 2. Architecture as built

| Component | Entry | Port | Verified |
|-----------|-------|------|----------|
| Server 1 AURA Core | `server1/app/main.py:create_app` | 8000 | yes (imports, tests) |
| Server 2 Brain+Storage | `server2/app/main.py` | 8001 | yes (imports, tests) |
| Server 2 storage-only | `server2/app/storage_app.py` | any | yes (integration tests) |

- Server 3 has been removed; storage now lives in Server 2 (`server2/app/storage/`,
  `server2/app/api/objects.py`, `server2/app/api/storage_health.py`).
- Server 1 mounts routers under `/api/v1` (`server1/app/api/v1/router.py`):
  health, auth, users, brain, identity, files, apps.
- Server 1 → Server 2: `server1/app/infrastructure/brain_client.py` (infer) and
  `server1/app/infrastructure/storage_client.py` (objects), both bearer-token
  authed with `INTERNAL_API_TOKEN`; user forwarded as `X-Aura-Owner-Id`.

### Classification
- Two-server split, routing, DI, settings: **implemented**.
- Server 2 storage subsystem: **implemented** (tested end-to-end with embedded PG).
- Server 2 inference: **implemented, mode-dependent** (GGUF / transformers /
  external Groq / canned test response when `AURA_SKIP_MODEL_LOAD=1` and no key).

---

## 3. Server inventory and endpoints

Server 1 (`/api/v1`):
- `health` — liveness/readiness aggregation (`app/api/v1/health.py`,
  `app/infrastructure/health.py`).
- `auth` — login, refresh, logout, me (`app/api/v1/auth.py`).
- `users` — registration, profile, lookup (`app/api/v1/users.py`).
- `brain` — query, ingest, memory CRUD + search (`app/api/v1/brain.py`).
- `identity` — AI identity info (`app/api/v1/identity.py`).
- `files` — proxy to Server 2 storage (`app/api/v1/files.py`).
- `apps` — catalog + per-user connections (`app/api/v1/apps.py`).

Server 2:
- `/health`, `/v1/brain/infer` (`app/main.py`).
- `/v1/objects*`, `/v1/storage/health`, `/v1/storage/ready`
  (`app/api/objects.py`, `app/api/storage_health.py`).

Status: **implemented**. Gaps are behavioral (below), not missing routes.

---

## 4. Database and migrations

Alembic chain under `server1/alembic/versions/`; head creates the full core
schema (`c382015c2dae_create_core_schema_tables.py`): `users`, `ai_identities`,
`devices`, `installed_modules`, `knowledge_graph`, `memory_vault`,
`module_permissions`, `personal_ai_brains`, `sessions`, `timeline`; plus
`brain_memories` in a later revision.

Classification: **implemented**. Note `sessions` and `devices` are created but
unused (see §6).

---

## 5. Authentication and security

- Password hashing: bcrypt (`app/core/security.py`). **implemented**.
- JWT access/refresh: HS256 via python-jose (`app/core/jwt.py`).
  Access TTL 15 min, refresh TTL 7 days (`app/core/config.py`).
- Bearer resolution: `app/dependencies/auth.py:get_current_user` validates
  `type=="access"` and loads the active user.
- Login updates `last_login_at`; **no session row is created**.
- Refresh: decodes token, returns **the same refresh token** — **no rotation**.
- Logout: validates the token then returns — **no revocation (no-op)**.
- Session model (`app/models/session.py`) has `refresh_token_hash`, `expires_at`,
  `is_revoked`, `revoked_at`, `last_used_at` — **unused**.

Classification: login/access-token enforcement **implemented**; session
persistence, refresh rotation, reuse detection, logout revocation **missing**.

Risks: stolen refresh token is valid for its full 7-day lifetime; logout does not
invalidate anything; no device/session listing or "revoke all".

---

## 6. Sessions and devices

Tables migrated, models present (`app/models/session.py`, `app/models/device.py`),
relationships defined. No repository, no service, no endpoint, no writes.

Classification: **missing** (schema-only).

---

## 7. Personal Brain — memory

Implemented:
- `BrainMemory` model with provenance `kind` = `fact` / `interpretation` /
  `event` and `source` = user/brain/app (`app/models/brain_memory.py`).
- Repository `app/repositories/brain_memory.py`: `get_recent_by_identity`,
  `search_by_identity` (**SQL `ILIKE` on content/summary**), `get_by_identity`.
- Service `app/services/personal_brain.py`: explicit
  `use_memory` / `persist_interaction` flags; facts vs interpretations kept
  separate; per-AI-identity scoping.
- API: create/list/get/update/delete/search memory (`app/api/v1/brain.py`).

Missing / partial:
- **Semantic retrieval: missing.** No embedding model, no vector store calls.
  `search_by_identity` is keyword-only.
- **`embedding_status` never transitions from `"pending"`** — no embedding job.
- `memory_vault` table: repository reads only (`get_brain_context`); **no
  writer**; ingestion routes to `MockPersonalVaultProvider`
  (`app/services/brain_provider.py`).
- `knowledge_graph` table: repository exists; **no writer**.
- `timeline` table: repository exists; populated only by reads; ingest does not
  write events.

Classification: structured memory **implemented**; semantic memory **missing**;
vault/knowledge-graph/timeline writing **missing**.

---

## 8. Embeddings and vector search (Qdrant)

- Client wrapper `app/infrastructure/qdrant/client.py` connects with
  `url`, `api_key`, `https=True`; supports embedded on-disk mode.
- Health probe `app/infrastructure/qdrant/health.py` returns safe reason
  categories (`forbidden`, `unauthorized`, `timeout`, `connection_error`).
- `collections.py:get_default_collection_names()` returns **static placeholder
  names** (`study_ai`, `meeting_ai`, ...) — no creation, upsert, or search.
- No embedding model dependency; no `qdrant_client.upsert`/`search` call sites.

Classification: connectivity + health **implemented**; embeddings and vector
read/write **missing**.

---

## 9. Brain inference (Server 2)

- `/v1/brain/infer` validates the internal bearer token, strips sensitive context
  keys (`vault`, `password`, `secret`, `token`, `jwt`), and returns
  model/latency/token metadata (`server2/app/main.py`, `app/brain/context.py`).
- Modes: local GGUF (llama.cpp), transformers checkpoint, external Groq proxy
  (`app/external_proxy.py`), or a canned test response when
  `AURA_SKIP_MODEL_LOAD=1` with no external key.
- Server 1 side: `RealPersonalBrainLLMProvider` calls Server 2; on failure, uses
  external OpenAI-compatible fallback when `AI_FALLBACK_ENABLED=true`
  (`app/services/real_brain_provider.py`), else returns a generic unavailable
  message.

Classification: **implemented, mode-dependent**; canned test mode must never be
presented as real inference.

---

## 10. Files / object storage

- Server 1 `/api/v1/files*` proxies to Server 2 with the authenticated user as
  owner (`app/api/v1/files.py`, `app/infrastructure/storage_client.py`).
- Server 2 storage: `local` / `s3` backends, metadata store, content-type and
  size limits (`server2/app/storage/`, `app/services/objects.py`).
- Readiness path corrected to `/v1/storage/ready`; config errors raise
  `StorageConfigurationError`; logging is secret-safe.

Classification: **implemented** (integration-tested against embedded PG).

---

## 11. Apps, modules and permissions

- Catalog of 16 apps/entries in `app/services/app_registry.py:CATALOG`;
  per-user connections via `InstalledModule` + `ModulePermission`.
- `has_active_permission` gates brain query/ingest by app slug + scope + action.
- Lifecycle: install / list / get / update (activate + replace permissions) /
  revoke.

Catalog status mix (see `docs/AURA_APP_INVENTORY.md`): `live` (aura-core, brain,
meeting, memory, files), `planned` (notes, study, tasks, notifications,
workflow, search), `module` (timeline, knowledge-graph, chronos), `future`
(health).

Classification: registry/permissions **implemented**; concrete app integrations
(Study Companion, Daily Diary, Meeting Assistant, Travel App, Notification Hub)
**missing/planned** — the catalog describes them but no ingestion pipelines exist.

---

## 12. Server 2 dead code / mock surface

Resolved by Phase F and the placeholder sweep (§18):

- Removed `server2/app/brain/*`, `server2/app/providers/brain_llm.py`,
  `server2/app/providers/vault.py`, `server2/app/schemas/brain.py`, and the
  duplicate `_call_external_model`. `server2/app/providers/` is now an empty,
  removed package.
- Remaining Server 2 mocks: inference runs in explicit *test mode*
  (`provider=test`, `source=server2_test_mode`) only when `AURA_SKIP_MODEL_LOAD`
  is set and no external provider is configured. Server 1 maps this to
  `source=server2_test_mode` so canned answers are never presented as real
  inference.
- Server 1 mock surface lives in `app/services/brain_provider.py`
  (`MockPersonalBrainLLMProvider`, `MockGeneralLLMProvider`,
  `MockPersonalVaultProvider`). It is selected only when `INTERNAL_API_TOKEN` is
  unset; the service now logs a warning (or an error under
  `ENVIRONMENT=production`) and the mock vault replies are tagged
  `development_only` (see §18).

Classification: **resolved**; mocks are unmistakable in responses.

---

## 13. Testing posture

- Server 1: unit tests for auth/brain boundary, memory, config/CORS, models,
  files, brain client/fallback, infrastructure health, qdrant client/health,
  storage client; integration tests boot Server 2 storage app + embedded PG
  (`server1/tests/`, `tests/integration/`).
- Server 2: storage object tests; model load skipped in CI.
- Latest local run: Server 1 `86 passed`; Server 2 `39 passed, 2 skipped`.

Gaps: no tests for session lifecycle/refresh rotation (feature missing), no tests
for embeddings/vector search (feature missing), no test asserting a Server-2
canned response is labelled as such end-to-end.

Classification: **under-tested** for the features that are missing.

---

## 14. Blocked / environment limitations

- No Docker daemon and no GPU in this workspace (per `AGENTS.md`): live
  `docker compose`, real 4B inference, and live endpoint checks are **blocked**.
- No production access: live Render `STORAGE_SERVER_URL`/`QDRANT_*` values cannot
  be verified; only category-level diagnostics are possible.
- Semantic memory design needs a decision: local embedding model vs external
  embeddings provider, and vector dimension (must be fixed before indexing).

---

## 15. Consolidated gap list (priority order)

1. Session lifecycle: persist sessions, rotate refresh tokens, reuse detection,
   real logout/revoke (security-critical). — Phase C
2. Semantic memory: embedding model + Qdrant collection creation, upsert on
   memory write, vector search with per-identity isolation. — Phase E
3. Vault / knowledge-graph / timeline writers. — Phase G/E
4. Remove Server 2 dead code; label mock/canned responses unambiguously. — Phase F
5. App ingestion pipelines for planned apps. — Phase I
6. Tests for the above; docs/deploy verification. — Phase J

## 16. Recommended next action

Implement Phase C (session lifecycle) first: it is security-critical, fully
unit/integration testable against embedded Postgres, and does not depend on the
embedding-model decision that blocks Phase E.

---

## 17. Remediation status (post-audit)

Implemented after this audit, with tests:

- **Phase C — session lifecycle (done).** New `app/repositories/session.py`;
  refresh tokens now carry a `jti` bound to a persisted session (SHA-256 hash
  only). Login persists a session; refresh rotates it; presenting a rotated
  token triggers reuse detection and revokes all of the user's sessions; logout
  revokes the session (idempotent). See `app/services/auth.py`,
  `app/core/jwt.py`, `app/core/security.py`. Tests: unit lifecycle +
  embedded-Postgres integration (`tests/test_auth_and_brain_boundary.py`,
  `tests/integration/test_backend_database.py`).
- **Phase E — semantic memory (done).** Deterministic offline embedding provider
  (`app/infrastructure/embeddings.py`), Qdrant vector store with mandatory
  per-identity payload filter (`app/infrastructure/qdrant/vector_store.py`), and
  an async `MemoryIndexer` with graceful keyword fallback
  (`app/services/memory_index.py`). Writes index vectors and set
  `embedding_status`; deletes remove vectors; search uses vector results first.
  Tests: `tests/test_semantic_memory.py` (relevance + identity isolation, real
  embedded Qdrant).
- **Phase F — dead code + honest inference labels (done).** Removed unused
  `app/brain/*`, `app/providers/brain_llm.py`, `app/providers/vault.py`,
  `app/schemas/brain.py`, and the duplicate `_call_external_model` in Server 2.
  Server 2 now labels every inference with `provider`/`source`, and Server 1 maps
  `provider=="test"` to `source == "server2_test_mode"` so canned responses are
  never mistaken for real inference.
- **Phase G — vault / knowledge-graph / timeline writers (done).** `ingest_data`
  now performs the side effects that `get_brain_context` reads: it upserts the
  identity's `memory_vault` pointer when the payload references a stored object
  (top-level or nested `metadata`); increments `knowledge_graph.node_count` for
  the ingest's classified labels; and appends a `timeline` `memory.ingested`
  event. New repository helpers `MemoryVaultRepository.upsert_for_identity` and
  `KnowledgeGraphRepository.record_nodes`; `vault_entry_id` surfaced on
  `BrainIngestResponse`. Also fixed a latent timezone bug in
  `TimelineRepository.get_recent_entries` (`datetime.utcnow()` →
  `datetime.now(timezone.utc)`). Tests: unit side-effect assertions plus
  embedded-Postgres integration (`test_brain_ingest_records_vault_knowledge_and_timeline`,
  `test_brain_ingest_without_object_leaves_vault_untouched`). Integration tests now
  force `MEMORY_VECTOR_SEARCH_ENABLED=false` so they never reach a real Qdrant.

- **Phase I — concrete application ingestion (done).** New
  `app/services/app_ingestion.py` with per-app normalizers and a registry
  (`study`, `diary`, `meeting`, `travel`, `notifications`, and `brain` for AURA
  Chat). `PersonalBrainService.record_ingestion` is the single write path
  (memory + vault pointer + knowledge node + timeline event, source
  `MemorySource.APP`); it adds idempotency via `external_id` and reports
  `partial` on side-effect failures while preserving the memory. New route
  `POST /api/v1/apps/{slug}/ingest` resolves the user server-side and enforces
  the connection's active permission; `POST /api/v1/brain/ingest` now also
  requires `memory:write` when `source_app` is supplied. New catalog entries
  `diary` and `travel`; `notifications` gains `notifications:write`. Tests:
  `tests/test_app_ingestion.py` (adapters + pipeline: dedupe, partial failure,
  no-identity) and integration tests (permission denial, cross-user isolation,
  idempotent replay, validation, notification timeline-only). No app frontends
  exist in the repository — documented in `docs/AURA_APP_INTEGRATIONS.md`.

New configuration: `MEMORY_VECTOR_SEARCH_ENABLED` (default true),
`QDRANT_MEMORY_COLLECTION` (default `brain_memories`), `EMBEDDING_DIMENSION`
(default 256).

Still open: the broader deployment verification (Phase J). The embedding
provider is a real but lexical (hashing) model; a neural provider with the same
dimension can be substituted for production-quality semantics.

---

## 18. Placeholder / mock / dead-code sweep

Classification key:
1. Safe configuration template (keep).
2. Test-only mock (keep; must be unmistakable).
3. Production placeholder (implement or remove from the production path).
4. Dead code / obsolete Server 3 reference (remove if nothing depends on it).
5. Optional integration (retain only if its optional behaviour is explicit).
6. Incomplete feature needing a design decision (document).

Findings:

| # | Location | Current behaviour | Production impact | Class | Action |
|---|----------|-------------------|-------------------|-------|--------|
| 1 | `server1/.env.example`, `server2/.env.example` | Documented env templates | None | 1 | Keep |
| 2 | `MockPersonalBrainLLMProvider` (`app/services/brain_provider.py`) | Offline canned answers; selected when `INTERNAL_API_TOKEN` unset | Silent mock answers if token missing | 2 | Kept; warning/error log added |
| 3 | `MockGeneralLLMProvider`, `MockPersonalVaultProvider` | No-op boundaries | Could imply success | 2 | Kept; tagged `development_only`; `stored` no longer derived from them |
| 4 | `server1/app/infrastructure/qdrant/collections.py` | Unused `get_default_collection_names()` "future" list | None (dead) | 4 | Removed |
| 5 | `server1/app/infrastructure/database/models.py` | Empty "placeholder for future models" module | None (dead) | 4 | Removed |
| 6 | `server2/app/core/config.py`, `server2/app/config.py` | Legacy `Config`/`settings` shim, incl. `USE_MOCK_MODEL`, `VAULT_ENABLED`, `SERVER1_URL` | Confusing duplicate config; unused | 4 | Removed |
| 7 | `server2/app/core/logging.py`, `server2/app/providers/` | Unused logger helper; empty package | None (dead) | 4 | Removed |
| 8 | `server1/app/infrastructure/embeddings.py` | Real deterministic hashing embeddings (dim 256), not neural | Lexical-only retrieval quality | 6 | Documented; neural provider is a design decision |
| 9 | `server2/app/storage/s3.py` | Real boto3 S3/MinIO backend, lazily imported | Only active when `STORAGE_BACKEND=s3` | 5 | Retained; optional and explicit (`boto3` pinned) |
| 10 | `server1/app/services/real_brain_provider.py:classify_information` | Deterministic label heuristic ("simple local classification") | Low; not ML classification | 6 | Documented as a design decision |
| 11 | `server1/app/services/app_registry.py` catalog `status="planned"/"future"` | Descriptive catalog of intended apps | None (catalog only) | 6 | Documented; only 6 apps have real ingestion adapters |
| 12 | `BrainIngestResponse.stored` | Previously derived from vault provider's optimistic reply | Could report success without a write | 3 | Fixed: now reflects the real write result |
| 13 | `BrainQueryResponse.source` default | Defaulted to `mock_personal_brain_provider` | Mock name in a production schema | 3 | Fixed: default `unknown` |
| 14 | Server 3 references | Only in historical docs/specs (`AUDIT_PHASE1.md`, `PLAN_PHASE2.md`, `IMPLEMENTATION_PLAN.md`, `server2/*SPEC*.md`, `aura-platform/*`) | None at runtime | 4 | Retained as historical record; no code path references Server 3 |
| 15 | `aura-platform/backend.worktrees/...` | Tracked legacy copy (90 files) with `TODO`s and `MockBrainProvider` | None at runtime; repo clutter | 4 | Documented; removal deferred (large legacy tree, no importers) |

Nothing depends on items 4–7 (verified by import/search); they were removed and
the test suites re-run. `.env.example` files were preserved. No secrets were read
or modified. Historical Server 3 documents were left intact because they are
records, not runtime references.
