# Personal Brain Harness Audit

## Scope and baseline

This audit reflects the repository currently in the workspace, not an
assumption that it still matches older three-service documentation. The active
deployable services are `server1/` and `server2/`; object storage, previously
called Server 3, is now hosted by Server 2. The old audit describing a separate
`server3/` deployment was stale.

Baseline commands were run before the harness changes:

| Suite | Result |
|---|---|
| Server 1: `cd server1 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests` | 194 passed, 19 failed |
| Server 2: `cd server2 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests` | 107 passed, 2 skipped |

The Server 1 failures cluster around endpoints returning `503 Personal Brain
is not ready`: those integration fixtures disable remote provisioning but
continue to exercise APIs that now require a ready Brain. These are baseline
failures, not caused by the Server 2 harness changes. Server 2's baseline was
green, with two skips. Docker, GPU inference, and the configured 4B checkpoint
are unavailable in this workspace as noted in `AGENTS.md`.

## Service and API map

### Server 1 — identity, permissions, and public gateway

- FastAPI entrypoint: `server1/app/main.py`.
- Authentication and account lifecycle: `server1/app/api/v1/auth.py`,
  `server1/app/api/v1/users.py`, and `server1/app/dependencies/auth.py`.
- Canonical user Brain and identity: `server1/app/models/personal_ai_brain.py`,
  `server1/app/models/ai_identity.py`, and their repositories.
- App catalog, consent, and per-app grants:
  `server1/app/services/app_registry.py` and
  `server1/app/api/v1/apps.py`.
- Query and application ingestion:
  `server1/app/api/v1/brain.py`,
  `server1/app/services/app_ingestion.py`, and
  `server1/app/services/personal_brain.py`.
- User-facing vault, files, and timeline APIs:
  `server1/app/api/v1/vault.py`, `files.py`, and `timeline.py`.
- Authenticated Server 2 contracts:
  `server1/app/infrastructure/brain_client.py` and
  `server1/app/infrastructure/storage_client.py`.

Server 1 authenticates end users, derives owner identity from the JWT, owns the
app catalog and grants, and is the public gateway. The user token is not sent
to Server 2. Server 1 forwards the internal service token and trusted owner
and Brain identifiers. App-facing requests must not select another user's
Brain.

### Server 2 — Brain runtime, private vault, and object storage

- Combined inference/storage application: `server2/app/main.py`.
- Storage-only ASGI entrypoint for tests and storage-only operation:
  `server2/app/storage_app.py`.
- Brain registry, lifecycle, and provisioning:
  `server2/app/brain_manager/registry/`,
  `lifecycle/manager.py`, and `provisioning/orchestrator.py`.
- Model artifact copy and runtime selection:
  `runtime/artifacts.py`, `runtime/factory.py`, and the runtime adapters.
- Per-Brain SQLite vault, vector index, consent snapshot, and migration:
  `vault/storage.py`, `vault/service.py`, `vault/apps.py`, and
  `vault/migration.py`.
- Object storage API and adapters: `server2/app/api/objects.py`,
  `server2/app/services/objects.py`, and `server2/app/storage/`.

Server 2 authenticates Server 1 with `INTERNAL_API_TOKEN`. The legacy object
storage role runs in the same service and remains behind Server 1's public
gateway. There is no active separate Server 3 deployment in this workspace.

## Capability assessment

| Capability | Assessment | Evidence and remaining gap |
|---|---|---|
| Canonical user identity and Brain ownership | Implemented; tested | Server 1 owns the user/identity relationship. Server 2 enforces unique owner and identity registry rows and filters vault operations on both Brain and owner. |
| Per-Brain manifest and provisioning | Implemented; tested | Provisioning is idempotent and records status/resources. Reconciliation is exposed through Server 1; private-vault migration is explicit and retryable. |
| Separate physical local model artifact | Partially implemented; tested for file copies | Dedicated local provisioning stages a separate copy and does not replace incomplete copies. Copy verification currently needs stronger integrity/hash and hard-link checks. Simulator and external-provider modes do not satisfy physical private weights; external weights remain provider-managed. |
| Isolated runtime and bounded concurrency | Implemented; tested at application level | Runtime IDs, contexts, and request slots are Brain-scoped. Runtime management is in-process; it is not OS/process/container isolation. |
| Private vault and object root | Implemented; tested | Each Brain has a separate SQLite file and local object directory. Backups/restores are scoped. SQLite and backups are not application-encrypted; deployment must provide OS/storage encryption. |
| App workspaces and consent enforcement | Implemented; tested | Vault namespaces preserve app provenance. Server 2 mirrors Server 1 consent and checks app grants at the storage boundary. |
| Original application data and timeline preservation | Partially implemented | Server 1 ingestion stores canonical app records and raw payload metadata, with provenance/timeline support. A uniform lossless raw-event envelope and distinct source-created/event/received/modified timestamps are not yet enforced across all adapters and vault records. |
| LLM retrieval and controlled tools | Implemented; focused tests added | Fixed search/list/read/create/update tools are bounded and audited. Normal mode enforces app grants and writes to the caller's namespace. Owner-approved `manage_vault` mode lets the internal Brain read and organize all namespaces represented in that owner's app connections; writes additionally require `confirm_tool_writes`. |
| Cross-app read access | Implemented with two explicit modes | Normal queries use app grants. Whole-vault management is an explicit per-request owner authorization, remains scoped to the owner's Brain, and includes inactive app connections. Server 2 verifies the source app's read grant and the owner/Brain boundary. |
| Update history and recoverability | Implemented for record updates; tested | Each successful update snapshots the prior version in the private vault; version history is owner/Brain-scoped and exposed through the Server 1 gateway. Tool-created/updated records carry AI-management metadata. The model tool surface has no delete operation. Backups and migration preserve vault resources. |
| Application-facing Server 2 access | Implemented; tested | End-user APIs stay on Server 1; internal Server 2 routes require the internal token and owner/Brain assertions. |

## Verification after this change

| Suite | Result |
|---|---|
| Server 1 complete suite | 198 passed, 19 failed (same failures as baseline) |
| Server 1 focused Brain authorization/context/version tests | 30 passed |
| Server 2 complete suite | 111 passed, 2 skipped |
| Server 2 Brain architecture/harness tests | 38 passed |

The 19 Server 1 failures are unchanged from baseline. Their common readiness
failure is consistent with the integration fixture setting
`BRAIN_REMOTE_PROVISIONING_ENABLED=false` and leaving newly created Brains
pending while tests exercise APIs that require a ready Brain. Fix the fixtures
to provision test Brains or assert the expected `503`; production readiness
must not be weakened to make those tests green.

The harness tests exercise authorized search, denial without explicit write
confirmation, confirmed/idempotent writes, whole-vault cross-app organization,
content-free audit events, AI metadata, and record-version preservation. Server
1 tests verify client-supplied tool authorization context is replaced with
gateway-derived values, manager namespaces are derived from the owner's app
connections, and version history is denied when the record belongs to a
different app namespace. The shared Android guide and reusable app-agent prompt
document the app-side opt-in and data contract.

## Operational and security limits

- Private local model copies consume roughly one full artifact per Brain. The
  0.8B GGUF is about 590 MB per copy; live runtime memory is additional and
  capacity-limited.
- External providers and simulator mode are not equivalent to per-user local
  physical model copies.
- Per-Brain folders and in-process runtime contexts provide application-level
  isolation, not separate operating-system users, processes, or containers.
- Vault SQLite files and backups are not encrypted by the application.
- Do not deploy until the internal token is set identically in Server 1 and
  Server 2, durable storage is configured, and the Server 1 baseline failures
  are resolved or explicitly accepted.
