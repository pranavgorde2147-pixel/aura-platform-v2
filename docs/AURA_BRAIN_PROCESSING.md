# AURA Brain — Universal Context and Routing Layer

Status: **partially implemented**. Server 2 analyzes inputs and stores records in
the authenticated user's Brain vault. Cross-app delivery, review actions,
processing-run retry/cancel, and automatic migration of legacy Server 1 content
are not implemented.

This document is the contract between the AURA Brain and every AURA
application. It describes what an application may send, what the Brain does
with it, how it decides where content belongs, what happens when it is not
sure, and how the platform keeps application data separate from Brain memory.

---

## 1. The model in one paragraph

An application submits content with its source identity. Server 1 validates the
caller's connection and permissions, Server 2 analyzes the content, and Server 1
stores the resulting segments in that user's Server 2 vault under
`apps/<source_app>`. Requests for other destinations are reported as blocked;
cross-app delivery is not implemented.

Understanding and personal vault storage live in Server 2. Server 1 handles
account authentication, app permissions, and the authenticated processing
gateway (`POST /api/v1/brain/process`).

```
   app ──POST /api/v1/brain/process──▶ Server 1
                                         │
             ┌───────────────────────────┼──────────────────────────┐
             │ 1. validate source app    │ 2. analyze (Server 2)     │
             │ 3. validate owner Brain    │ 4. store source segments │
             │ 5. report blocked targets  │                          │
             └───────────────────────────┴──────────────────────────┘
                     │                     │
                     ▼                     ▼
                   Server 2 Brain vault
                    apps/<source_app>
```

---

## 2. Endpoints

### 2.1 `POST /api/v1/brain/process`

Submit one input for understanding, routing and delivery.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `content` | string (1..200000) | — | The text to process. |
| `input_id` | string | uuid | Stable client-side id for provenance. |
| `input_type` | string | `text` | Free-form type of the input. |
| `source_app` | string | — | The submitting application. Validated server-side. |
| `content_ref` | string | — | Protected reference to content already stored (object id, upstream record id). |
| `occurred_at` / `captured_at` | datetime | — | When the event happened / was captured. |
| `language` | string | — | Language hint for the understanding layer. |
| `session_id` | string | — | Conversation/session grouping. |
| `context` | object | `{}` | Extra context for the understanding layer (never trusted for authorization). |
| `consent.memory_write` | bool | `false` | Allow this input to contribute to Brain memory. |
| `consent.retain_source` | bool | `true` | Allow retention of the original content. |
| `consent.cross_app_routing` | bool | `false` | Explicitly allow references/routing to apps other than the source. |
| `use_memory` / `memory_limit` | bool / int | `true` / `5` | Whether existing memory may be consulted. |
| `persist_memory` | bool | `false` | Explicit opt-in to write memory. |
| `requested_destinations` | string[] | `[]` | Restrict delivery to these apps (still validated). |
| `idempotency_key` | string | — | Body-level alternative to the `Idempotency-Key` header. |

Responses: `201` with a `ProcessingResponse`, `403` when the source app is not
authorized, `422` for invalid destinations, and `503` when Server 2 is
unavailable or returns unusable results.

### 2.2 `GET /api/v1/brain/process/{processing_id}`

Returns the current state of a run owned by the caller. A run belonging to
another user is simply not found.

### 2.3 `POST /api/v1/brain/process/{processing_id}/confirm`

Currently returns `410 Gone`. Cross-app delivery and review actions are not
implemented.

### 2.4 `POST /api/v1/brain/process/{processing_id}/retry`

Currently returns `410 Gone`. Per-run delivery retries are not implemented.

### 2.5 `POST /api/v1/brain/process/{processing_id}/cancel`

Currently returns `410 Gone`. Delete stored records through the confirmed
Brain-vault API instead.

### 2.6 `POST /v1/brain/analyze` (Server 2, internal)

The understanding service. Internal bearer token required. Returns the
segments, the analysis mode and the confidence kind:

```json
{
  "analysis_mode": "llm",
  "confidence_kind": "model",
  "provider": "local",
  "degraded": false,
  "degraded_reason": null,
  "segments": [
    {
      "index": 0,
      "category": "lecture",
      "record_type": "lecture_note",
      "text": "Lecture on normalisation.",
      "summary": "A lecture covering the topic of normalisation.",
      "entities": ["normalisation"],
      "occurred_at": null,
      "confidence": 0.95,
      "rationale": "Explicit mention of a lecture topic."
    }
  ]
}
```

`analysis_mode` is `llm` when the model produced the labels and `deterministic`
when the dependency-free structural analyser was used. `confidence_kind` is
`model` (calibrated by the model) or `heuristic` (a structural estimate).
**Heuristic confidences are never presented as calibrated scores**, and the
Server 1 response carries the same label to the client. When the model was
tried but its answer could not be used, the response is marked
`degraded: true` with a `degraded_reason` (`model_load_skipped`,
`llm_output_unusable`) and the labels come from the structural analyser.

The analysis response supplies categories and record types, which are retained
as metadata on the stored vault segments.

---

## 3. How a destination is chosen

A destination is never derived from the raw text. It comes from the
application registry (`server1/app/services/app_registry.py`), where each entry
declares:

| Field | Meaning |
|-------|---------|
| `categories` | Semantic categories the application accepts. |
| `record_types` | Record types the application can store. |
| `routable` | Whether the Brain may route records into the application at all. |
| `storage_adapter` | Catalog metadata describing the app integration; new processing records are stored in the Server 2 Brain vault. |
| `ingest_scope` / `ingest_action` | Permission an app needs to *submit* content. |
| `write_scope` / `write_action` | Permission an app needs to *receive* records. |

The planned cross-app routing design remains unimplemented. Current processing
behavior validates the source app and write permission, analyzes the input in
Server 2, and stores the resulting segments under `apps/<source_app>` in that
owner's vault. Non-source destinations are reported as blocked; no cross-app
write takes place.

`GET /api/v1/brain/process/{processing_id}?client_app=<slug>` reconstructs
status from the caller's authorized Server 2 namespace. Confirm, retry, and
cancel currently return `410 Gone`.

---

## 4. Classification and cross-app review

**Current behavior:** Server 2 returns classification and confidence metadata,
but the processing endpoint does not apply the older risk thresholds below to
route records. It stores each segment under the source app's namespace.
Non-source destinations are reported as blocked; confirm/review operations
currently return `410 Gone`.

Confidence thresholds are policy, not model output. They live in
`server1/app/core/config.py` and are applied per risk tier:

| Risk tier | Categories | Threshold (default) |
|-----------|-----------|---------------------|
| low | `note`, `document`, `project` | `BRAIN_ROUTING_CONFIDENCE_NOTE` = 0.50 |
| medium | `study_material`, `lecture`, `meeting`, `task`, `deadline`, `travel`, `diary_event` | `BRAIN_ROUTING_CONFIDENCE_MEDIUM` = 0.70 |
| high | `health`, `calendar_event`, `personal_fact`, `user_preference`, `unclassified` | `BRAIN_ROUTING_CONFIDENCE_SENSITIVE` = 0.85 |

- An item at or above the threshold for its tier is delivered automatically.
- An item below the threshold becomes `awaiting_review`; the run reports
  `review_required: true` and the client is expected to ask the user.
- `unclassified` is **never** delivered automatically, at any confidence.
- High-risk categories additionally set `requires_confirmation` on the
  operation.
- When the understanding service cannot be reached, the item is reported as a
  single `unclassified` segment with confidence `0.0` and the run is marked
  degraded. The Brain does not guess.

`BRAIN_ROUTING_AUTO_DELIVER=false` turns automatic delivery off entirely; every
item then waits for confirmation.

---

## 5. Unified Brain vault

**Current behavior:** New app records, processed segments, document text,
explicit memories, and conversation turns are stored in the owner's Server 2
Brain vault in logical namespaces such as `apps/study`, `documents`, `shared`,
and `conversations`. The prior Server 1 `app_records` and `brain_memories`
design described below is legacy; new data must use `/api/v1/brain/vault`.
Server 1 content has not been migrated or deleted. Migration needs a separate
backup, verification, and approval.

Cross-app *reads* are supported separately from cross-app *delivery*: Brain
queries and vault search include another active app's namespace only when the
owner has granted that app `brain:read`; uploaded document content additionally
requires the Files app's `files:read`. A processing request still writes to its
source app namespace and does not copy or route data into another app's
namespace.

The Server 2 internal token and owner header are server-side credentials and
must never be sent by Android clients. Use Server 1's authenticated gateway
for vault reads, search, updates, and confirmed soft-delete.

Queries that include a catalog `source_app` store their query/answer turn under
`apps/<source_app>` for app-wise organization. Queries without a `source_app`
use `conversations`. Query persistence is automatic: the user query is saved
before inference, and the answer is added after successful inference. Legacy
clients may still send `persist_interaction:false`, but the field is ignored.
If inference fails, the query remains in the private vault with a pending
status and can be retried using the same `request_id`.

<!-- Legacy architecture notes below are retained for historical context. -->

These are deliberately different things.

| | `app_records` | `brain_memories` |
|---|---|---|
| Purpose | The permanent records of an application (lecture notes, meeting transcripts, diary entries, tasks, trips, documents) | The short, personal context used for future reasoning |
| Content | The **full** content of the item | A **bounded summary** only |
| Owner | The user, partitioned by `destination_app` and `record_type` | The user's AI identity |
| Write path | The routing pipeline and the legacy ingestion path | Opt-in only (`persist_memory` + `consent.memory_write` + policy) |
| Retention | `retention_policy` + `expires_at` | `retention_policy` + `expires_at` |

- Brain memory is never written without an explicit opt-in, and only for
  `personal_fact` and `user_preference` items. Meeting transcripts, diary
  entries, health notes, study material and documents are never turned into
  conversational memory.
- Content that looks like a credential (password, api key, token, private key,
  card number) is never stored in memory, even when it was opted in.
- The legacy ingestion path (`POST /api/v1/apps/{slug}/ingest`) was changed to
  follow the same rule: the full content goes to `app_records`, the memory
  keeps the bounded summary. The `memory_id` in the response is unchanged for
  compatibility; `record_id` is the new field that points at the full record.

### Shared record vault and cross-app references

Routable source apps can retain one canonical source record with its original
content, structured categories/topics, provenance, expiry policy, and
processing status. Classification adds metadata without replacing that
content. When the user explicitly opts in to cross-app routing, authorized
destination apps receive an owner-scoped link to the canonical record instead
of a duplicate copy. With `consent.retain_source=false`, legacy destination
storage behavior applies; no canonical source record is created.

The authenticated Server 1 `/api/v1/records` API provides list, search, detail,
related-link, create-link, and delete operations. Every call supplies a
`client_app`, which must be a registered active connection with the requested
`records:read`, `records:write`, or `records:delete` grant. Each returned source
also requires its own active `<source-app>:read` permission. Deletion requires
both `records:delete` on the calling app and `<source-app>:delete` on the
record's source. The authenticated user is always derived from JWT; request
parameters cannot choose an owner. Cross-app links additionally require the
destination app's registry-declared write permission. The API never grants
these permissions automatically.

List and search responses omit content unless `include_content=true`; fetching
one record by ID returns its content. Object-backed records keep their original
bytes in Server 2 storage and expose only the object reference through this
API; retrieve them through the existing owner-scoped files proxy. Brain query
retrieval can hydrate bounded text objects before sending them to inference;
unreadable or unsupported selected objects produce an explicit service error,
not an empty-content answer.

---

## 6. Idempotency, retries and duplicates

- `Idempotency-Key` (header) or `idempotency_key` (body) is scoped to the
  authenticated user. A replay returns the **original run** with the original
  `processing_id` instead of creating a second one.
- Every record carries a `dedupe_key` derived from
  `(user, destination, record type, run, segment, content hash)`. Writing the
  same key twice resolves to the same stored record, so a retried delivery
  never duplicates content.
- Operations are tracked individually, so a partial delivery can be retried
  without repeating the parts that already succeeded.

---

## 7. Response shape

```jsonc
{
  "input_id": "lecture-1",
  "processing_id": "0f0b…",
  "source_app": "study",
  "status": "awaiting_review",
  "items": [
    {
      "index": 0,
      "category": "lecture",
      "record_type": "lecture_note",
      "summary": "A lecture covering the topic of normalisation.",
      "confidence": 0.55,
      "confidence_kind": "heuristic",
      "entities": ["normalisation"],
      "source_reference": "objects/lecture-1",
      "destination_app": "study",
      "status": "awaiting_review",
      "requires_review": true,
      "rationale": "Matched only a weak topical cue."
    }
  ],
  "classification": {
    "analysis_mode": "deterministic",
    "confidence_kind": "heuristic",
    "segment_count": 1,
    "categories": ["lecture"],
    "thresholds": {"lecture": 0.7}
  },
  "destinations": [ { "destination_app": "study", "record_type": "lecture_note",
                      "category": "lecture", "confidence": 0.55,
                      "status": "awaiting_review", "candidates": ["study"] } ],
  "authorization": [ { "destination_app": "study", "record_type": "lecture_note",
                       "allowed": true, "reason": null,
                       "required_scope": "study", "required_action": "write",
                       "required_permission": "study:write" } ],
  "memory_operations": [ { "action": "skipped", "item_index": 0,
                           "reason": "memory_write_not_requested" } ],
  "delivery": [ { "destination_app": "study", "record_type": "lecture_note",
                  "status": "pending", "attempts": 0 } ],
  "record_references": [],
  "review_required": true,
  "errors": [],
  "idempotency_key": "client-key-1"
}
```

Errors are always reported as `{code, message, stage}` with safe messages: no
credentials, no upstream bodies, no internal hostnames.

### Statuses

An item may have several destinations. `items[].status` reports the **best**
outcome among them — `delivered`, `ready_for_delivery`, `awaiting_review`,
`awaiting_authorization`, `failed`, `skipped` — while `destinations[]`,
`authorization[]` and `delivery[]` keep the per-destination state. An item that
was delivered to one application while another destination is unauthorized is
reported as `delivered`; the blocked destination stays visible in
`authorization[]` with `allowed: false`, the reason and the permission that
would be needed (`required_permission`).

---

## 8. How an application integrates

1. **Connect** the user to the application:
   `POST /api/v1/apps/me` with `{"module_slug": "study"}`. The default
   permissions of the catalog entry are granted; the user may narrow them.
2. **Submit** content:
   `POST /api/v1/brain/process` with `content` and `source_app`. Use an
   `Idempotency-Key` so a retry of the same user action does not create a
   second record.
3. **Read the outcome**: `status`, `items[].status`, `review_required`.
4. **Ask the user** when `review_required` is true, then call
   `POST /api/v1/brain/process/{id}/confirm` with the approved item indexes.
5. **Reconnect** after a transient failure with
   `POST /api/v1/brain/process/{id}/retry`.

Cross-app routing defaults to off. An app must send
`"consent":{"cross_app_routing":true}` for routing outside its source app, and
each target still needs an active connection and its registry-declared write
permission. A stable `input_id` or `Idempotency-Key` should be used for every
retry. Reusing a `content_ref`/`input_id` for different content returns `409`
rather than overwriting the source record.

A minimal client call:

```bash
curl -X POST https://api.aura.local/api/v1/brain/process \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"content":"Lecture on normalisation. The deadline is on Friday.",
       "source_app":"study","input_id":"lecture-42"}'
```

Applications that only want to hand over an event and let the Brain decide
never pass `requested_destinations`. Applications that want a specific target
may pass it; the Brain still authorizes it.

---

## 9. Configuration

Server 1 (`server1/.env`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `BRAIN_ROUTING_ENABLED` | `true` | Master switch for the processing pipeline. |
| `BRAIN_ROUTING_AUTO_DELIVER` | `true` | Allow automatic delivery above the threshold. |
| `BRAIN_ROUTING_CONFIDENCE_NOTE` | `0.50` | Threshold for low-risk categories. |
| `BRAIN_ROUTING_CONFIDENCE_MEDIUM` | `0.70` | Threshold for medium-risk categories. |
| `BRAIN_ROUTING_CONFIDENCE_SENSITIVE` | `0.85` | Threshold for high-risk categories. |
| `BRAIN_ROUTING_MEMORY_WRITE_ENABLED` | `true` | Master switch for memory writes. |
| `BRAIN_ROUTING_MEMORY_SUMMARY_MAX_CHARS` | `1000` | Maximum length of a memory summary. |
| `BRAIN_ROUTING_MEMORY_RETENTION_DAYS` | `0` | Memory expiry; `0` means "until deleted". |
| `BRAIN_ROUTING_RECORD_RETENTION_DAYS` | `0` | Application record expiry; `0` means "until deleted". |
| `BRAIN_ROUTING_MAX_DELIVERY_RETRIES` | `3` | Delivery attempts per operation. |
| `BRAIN_ROUTING_MAX_SEGMENTS` | `12` | Maximum items extracted from one input. |
| `BRAIN_ROUTING_ANALYSIS_TIMEOUT_SECONDS` | `60` | Timeout for the understanding call. |
| `BRAIN_ROUTING_IDEMPOTENCY_REQUIRED` | `false` | Require an idempotency key. |

Server 2 keeps the understanding behaviour (`AURA_SKIP_MODEL_LOAD`,
`ANALYSIS_MAX_NEW_TOKERNS`, `EXTERNAL_AI_URL`/`EXTERNAL_AI_KEY`).

---

## 10. Database

| Table | Purpose |
|-------|---------|
| `brain_processing_runs` | One row per submitted input: state, idempotency key, bounded summary, provenance. Never a second copy of the raw content. |
| `brain_processing_operations` | One row per planned destination operation, with status, attempts, error code and the extracted item payload used for (re)delivery. |
| `app_records` | The application-owned records, partitioned by destination and record type, with retention and provenance. |
| `brain_memories` | Unchanged, plus `retention_policy` and `expires_at`. |

Migrations:

- `server1/alembic/versions/e6a49cbd1731_add_shared_record_metadata_and_links.py`
  adds structured metadata fields and the owner-scoped link table.
- `server1/alembic/versions/f1a2b3c4d5e6_add_brain_resource_registry.py`
  adds per-user Brain provisioning and resource metadata.

Roll out with `alembic upgrade head` before deploying Server 1.

---

## 11. Operational notes

- **Understanding is optional at runtime.** If Server 2 is unreachable, the
  pipeline degrades to `unclassified` items that require review instead of
  failing the request. No content is lost.
- **Object-backed destinations need the storage service.** The `files`
  application uses the `object` adapter and therefore requires Server 2's
  storage API to be reachable.
- **Confidence is not a probability.** The thresholds are policy boundaries
  chosen by the platform; the `confidence_kind` field tells the client whether
  the score came from the model or from the structural analyser.
- **The pipeline never bypasses permissions.** A missing grant becomes an
  explicit `awaiting_authorization` operation, never a silent skip.

## 12. Per-user Brain resource registry

Every Server 1 account has one `AIIdentity` and one `PersonalAIBrain` row,
created in the account transaction. Server 1 uses a stable Brain UUID as the
canonical resource identity and asks Server 2 to idempotently register the
owner, identity, model configuration, storage namespace, and bounded request
capacity. Registration failure leaves the account intact with `pending` or
`failed` status and a safe error code; it can be retried without replacing the
Brain or identity.

Authenticated owners can inspect `GET /api/v1/brain/me` and retry provisioning
with `POST /api/v1/brain/me/reconcile`. To enable metadata-only operator views,
configure `BRAIN_ADMIN_USER_IDS` as a comma-separated list of trusted user UUIDs.
The allowlisted admin can use `GET /api/v1/admin/brains` and
`POST /api/v1/admin/brains/reconcile?limit=20`; an empty allowlist grants no
admin access. The endpoints expose resource metadata, not user memory or
record contents.

### Deployment and recovery

1. Configure matching `INTERNAL_API_TOKEN` values for Server 1 and Server 2.
   Set Server 1 `AI_SERVER_URL` and `AI_SERVER_MODEL` to the Server 2 origin and
   the exact model name configured by Server 2 (`MODEL_NAME` or its documented
   default). Set Server 2 `STORAGE_DATABASE_URL` to persistent storage.
2. Apply Server 1 migrations with `alembic upgrade head`, then deploy Server 2
   with the Brain registry code. Its storage startup creates the
   `managed_brains` table and reconciles runtime entries left behind by a
   previous Server 2 process. Run one Server 2 process per shared registry;
   runtime handles are process-local.
3. Deploy Server 1 with `BRAIN_REMOTE_PROVISIONING_ENABLED=true` (the default).
   New registrations provision after the account transaction commits. Existing
   users are not remotely registered just because the migration ran.
4. Each user can authenticate and call `POST /api/v1/brain/me/reconcile`. An
   operator may instead set `BRAIN_ADMIN_USER_IDS` and run the bounded admin
   reconcile endpoint. Check `GET /api/v1/brain/me` or the admin list for
   `provisioning_status: "ready"`. This confirms registration and namespace
   creation, not model health.
5. Verify a real authenticated Brain query end to end. The Server 2
   `last_health_check` is populated only after an inference/analysis request
   completes without a server error. A `ready` registry entry alone is not
   evidence that inference succeeded.

### Isolation boundary and current capacity model

Brain IDs, AI identities, relational memories, user records, and object-storage
keys are owner-scoped. Brain-associated object keys use
`owners/{owner_id}/brains/{brain_id}/...`; Server 2 rejects an identity that
has not been registered to that owner.

**Every Brain owns a dedicated LLM runtime.** Server 2 holds the authoritative
registry and creates one runtime object per Brain (`llm_runtime_id`, unique
across all Brains). What "dedicated" means depends on the configured runtime
mode, and the service always reports the real isolation level in
`runtime_isolation` rather than a flattering one:

| Mode | Isolation reported | What is actually dedicated |
|------|--------------------|------------------------------|
| `dedicated` + llama.cpp | `dedicated_weights` | One `llama_cpp.Llama` instance per Brain loads an independent model-weight copy, context, KV cache and sampling state. |
| `dedicated` + transformers | `dedicated_weights` | One `AutoModelForCausalLM` instance per Brain: its own weights, KV cache and tokenizer state. |
| `external` | `context_isolated_shared_weights` | One provider handle and one context per Brain; the provider's weights are shared. |
| `simulated` | `simulated` | A deterministic simulator with a real per-Brain runtime object, context and cache. Never presented as model output. |

There is **no shared-runtime fallback**. When a Brain's dedicated runtime
cannot be created, the registry records an honest state
(`failed`, `resource_limited`, `waiting_for_resources` or `stopped`) with a
stable error code, and the inference path answers `503` instead of serving the
request from another Brain's runtime. There is no configuration switch that
re-enables this: a Brain is only ever served by its own runtime.

Each Brain additionally owns its own memory namespace, vector index, personal
vault, connected-app authorization snapshot, processing-job queue and compute
accounting (`request_count`, `max_concurrent_requests`).

**Capacity is per process.** `BRAIN_MAX_CONCURRENT_RUNTIMES` bounds how many
Brains may hold a live runtime in one Server 2 process, and
`BRAIN_MAX_CONCURRENT_REQUESTS_PER_BRAIN` bounds the concurrent requests of a
single Brain (enforced by an in-process semaphore). This is not a cross-worker
quota and not per-user hardware allocation: a multi-process or multi-host
deployment needs a shared scheduler (or sticky routing plus a shared capacity
store) before it can claim that level of isolation. The
`dedicated_weights` mode in particular costs real memory per Brain, so the
capacity limit must be sized against the deployment's memory budget.

### Administration

Server 2 exposes a metadata-only administrative registry API under
`/v1/admin/brains`. It requires both the shared internal service token and an
owner id on the `BRAIN_ADMIN_OWNER_IDS` allowlist. An empty allowlist disables
the API entirely. The administrative endpoints expose registry metadata,
capacity and health — never vault contents, records or user memory.

Server 1 exposes the matching operator endpoints under `/api/v1/admin/brains`,
gated by `BRAIN_ADMIN_USER_IDS`.

---

## 13. Server 2 Brain APIs

All Brain endpoints require the shared internal service token
(`Authorization: Bearer $INTERNAL_API_TOKEN`). Endpoints that touch user data
additionally require the owner id that Server 1 forwarded in
`X-Aura-Owner-Id`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/brains/provision` | Register (idempotently) a Brain and give it a dedicated runtime and private resources. |
| `GET` | `/v1/brains/{brain_id}` | Return the registry entry of one Brain. |
| `GET` | `/v1/brains/{brain_id}/manifest` | Return the secret-free resource manifest of one Brain. |
| `GET`/`POST` | `/v1/brains/{brain_id}/vault/records` | List or create the Brain's private vault records. |
| `GET`/`DELETE` | `/v1/brains/{brain_id}/vault/records/{record_id}` | Read or delete one record. |
| `GET`/`POST` | `/v1/brains/{brain_id}/vault/records/{record_id}/relationships` | Read or add the record's relationship graph. |
| `POST` | `/v1/brains/{brain_id}/vault/search` | Semantic search inside the Brain's own vector index. |
| `GET` | `/v1/brains/{brain_id}/vault/status` | Report the state of the vault and the vector index. |
| `GET`/`PUT`/`DELETE` | `/v1/brains/{brain_id}/apps[/{app_slug}]` | Read, record or revoke the Brain's connected-app authorization snapshot. |
| `POST`/`GET` | `/v1/brains/{brain_id}/jobs` | Enqueue or list the Brain's processing jobs. |
| `POST` | `/v1/brains/{brain_id}/jobs/drain` | Run the Brain's queued jobs inside that Brain's context. |
| `POST` | `/v1/brain/infer` | Run inference on the Brain's own dedicated runtime. |
| `POST` | `/v1/brain/analyze` | Structured understanding on the Brain's own dedicated runtime. |
| `GET` | `/v1/admin/brains[/{brain_id}]` | Administrative registry metadata (allowlisted admins only). |
| `GET` | `/v1/admin/brains/cluster` | Cluster capacity and health picture. |
| `GET` | `/v1/admin/brains/health` | Health check of every registered Brain. |
| `POST` | `/v1/admin/brains/reconcile` | Reconcile the registry with the runtimes that actually exist. |
| `POST` | `/v1/admin/brains/{brain_id}/runtime/stop` | Stop one Brain's dedicated runtime. |
| `POST` | `/v1/admin/brains/{brain_id}/runtime/restart` | Restart one Brain's dedicated runtime. |

### Owner isolation

Every Brain-scoped read and write is filtered on `brain_id` **and** `owner_id`.
Asking for another owner's Brain returns `404`, never `403`, so the API never
confirms that another user's Brain exists. The inference path resolves the
Brain from the registry using the authenticated `user_id` — the request body
cannot re-target a request at another Brain.

### Environment variables (Server 2)

| Variable | Default | Meaning |
|----------|---------|---------|
| `BRAIN_RUNTIME_MODE` | auto | `dedicated`, `external` or `simulated`. Empty means: `external` when `AURA_SKIP_MODEL_LOAD=1` and an external provider is configured, `simulated` when no model can be loaded, `dedicated` otherwise. |
| `BRAIN_RUNTIME_ADAPTER` | `auto` | `auto`, `llama_cpp`, `transformers` or `simulated`. |
| `BRAIN_MAX_CONCURRENT_RUNTIMES` | `8` | How many Brains may hold a live runtime in this process. |
| `BRAIN_MAX_CONCURRENT_REQUESTS_PER_BRAIN` | `1` | Concurrent requests served for one Brain. |
| `BRAIN_MAX_CONTEXT_TOKENS` | `8192` | Context budget of one Brain's runtime. |
| `BRAIN_RESOURCE_ROOT` | `./data/brains` | Root of the per-Brain resource directories. |
| `BRAIN_MAX_JOBS_PER_BRAIN` | `64` | Maximum queued jobs per Brain. |
| `BRAIN_JOB_MAX_ATTEMPTS` | `3` | Attempts before a job is marked failed. |
| `BRAIN_ADMIN_OWNER_IDS` | empty | Owner ids allowed to call `/v1/admin/brains`. Empty disables the administrative API. |
