# AURA Server 2

AURA Server 2 is the per-user Personal Brain runtime (local Qwen3.5-0.8B GGUF or an external provider) and hosts the object-storage API that Server 1's file API proxies to.

## Purpose

Server 2 is the execution boundary for each Personal Brain. Server 1 creates a user's account, default identity and canonical Brain record, then provisions that Brain on Server 2. The private vault is created inside that Brain's `BRAIN_RESOURCE_ROOT` directory. No shared/pre-created user Brain is created on startup.

The manifest includes the owner's display name and resource references, but never an access token, password, API key or other credential. User authentication stays in Server 1; Server 2 accepts only the shared server-to-server credential and the owner/Brain IDs asserted by Server 1.

## Model source of truth

The local model artifact is resolved from `MODEL_PATH`, then `/models/`,
`server2/models/`, and the `server2/` root. The default local model is
Qwen3.5-0.8B in GGUF format (`Qwen3.5-0.8B-Q4_K_M.gguf`). A Qwen3.5-4B
transformers checkpoint is also supported when configured. When a new Brain
is provisioned in local `dedicated` mode, Server 2 copies the complete
configured artifact into that Brain's `personal_llm/model_weights/` directory
before starting its runtime. Copies are staged and atomically installed;
retries reuse an existing complete copy and do not overwrite it. The source
artifact is retained. Each Brain runtime loads its own private copy and has
its own inference context. This uses additional disk space per user (for
example, about 590 MB per copy of the configured 0.8B GGUF); ensure there is
enough free space for the expected accounts. A missing source or failed copy
leaves provisioning failed rather than silently sharing model weights.

The model is never copied into the Docker image.

## Runtime behavior

- Server 2 authenticates the internal Server 1 bearer token.
- The request is validated for UUIDs, non-empty query, correct model name, and approved context.
- The service builds a deterministic prompt using approved context and safety constraints only.
- Each Brain has its own runtime ID and inference context. Local runtime mode creates an individual Qwen runtime for each Brain; external-provider mode isolates Brain context but necessarily shares provider weights.
- Brain runtime, memory, vector and vault resources are scoped by the canonical Brain and owner IDs; a failed runtime is reported as unavailable rather than borrowed from another user.
- The model runs on CPU unless CUDA is explicitly available.
- No model runtime is loaded on service startup. A user's runtime is created
  only when Server 1 provisions that user's Brain after signup.
- `/health` and `/ready` report API availability without loading a model.
  `model_loaded:false` and `active_brain_runtimes:0` are expected before the
  first account is provisioned.

## Account provisioning and private Brain files

`POST /api/v1/users` creates the user's default identity and canonical Brain in
Server 1, then calls Server 2's service-authenticated `POST /v1/brains/provision`.
Server 2 creates or converges on that user's resource tree and writes its
manifest to the configured `BRAIN_RESOURCE_ROOT`. New Brain directories include
the sanitized account display name and unique Brain UUID; retries retain the
existing directory reference. Existing UUID-named resource directories are
left in place and are not automatically deleted or renamed. For local
GGUF/transformers mode, each Brain gets its own full model copy and dedicated
inference runtime as part of provisioning.

Relative `BRAIN_RESOURCE_ROOT` paths are resolved from the Server 2 directory
(default `server2/data/brains`); Docker Compose uses `/var/data/brains`.
`BRAIN_VAULT_BACKUP_ROOT` defaults to `server2/brain_vault_backups` and remains
outside the live Brain directory.
The private data layout is:

```text
<BRAIN_RESOURCE_ROOT>/
└── <registered-brain-folder>/
    ├── brain_manifest.json
    ├── personal_llm/
    │   └── model_weights/       # Existing private copy, if present
    ├── memory_vault/
    │   └── vault.sqlite
    ├── objects/
    └── indexes/
```

Vault records, app data, memories, relationships, embeddings, processing jobs,
conversation history, and object metadata are persisted in that Brain's
`memory_vault/vault.sqlite`. Uploaded bytes are stored beneath its `objects/` directory;
the `indexes/` directory is reserved for Brain-local derived indexes. The
metadata database configured by `STORAGE_DATABASE_URL` retains the trusted
Brain registry and operational authorization/manifest/migration state, not
vault record bodies. Creating another Brain resolves to another vault file and
object root.

### Existing shared-vault data migration

Existing Server 2 data may still be in the legacy shared metadata database.
The new code does not delete or automatically copy it. First inventory a
registered Brain, then explicitly migrate it from the Server 2 directory:

```bash
cd server2
.venv/bin/python -m app.brain_manager.vault.migration --brain-id <BRAIN_UUID> --dry-run
.venv/bin/python -m app.brain_manager.vault.migration --brain-id <BRAIN_UUID>
```

The migration copies only rows whose `brain_id` and `owner_id` match the
Server 2 registry, copies object bytes and verifies checksums, is safe to retry,
and records its status in the metadata DB. Ambiguous or missing objects are
reported for review. The old shared database and object files are retained;
do not clean them up until each Brain's migration status is `verified`, counts
and object checksums have been independently checked, and cleanup is explicitly
approved. Back up the full `BRAIN_RESOURCE_ROOT` and metadata database before
operator-approved changes. The operator CLI backs up and restores only
`memory_vault/`, `objects/` and `indexes/`; it preserves the Brain manifest and
any model files in `personal_llm/`.

For a consistent per-Brain backup/restore or explicit vault-only deletion, use
the local operator CLI (always pass the registry-matching Brain and owner IDs):

```bash
.venv/bin/python -m app.brain_manager.vault.manage backup --brain-id <BRAIN_UUID> --owner-id <OWNER_UUID>
.venv/bin/python -m app.brain_manager.vault.manage restore --brain-id <BRAIN_UUID> --owner-id <OWNER_UUID> --backup-id <BACKUP_UUID>
.venv/bin/python -m app.brain_manager.vault.manage delete --brain-id <BRAIN_UUID> --owner-id <OWNER_UUID> --confirm-delete
```

SQLite and these backups are **not encrypted by this application**. Protect
the laptop and both configured storage roots with OS-level full-disk encryption
and restrictive account access; if portable encrypted backups are required,
encrypt them with an approved key-management solution before moving them off
the host. The private-vault implementation does not invent or persist
per-user encryption keys.

There is intentionally no per-user bearer token stored in the Brain file.
Server 1 authenticates the user and scopes requests to their Brain; the shared
internal token authenticates only Server 1 to Server 2. `POST /api/v1/brain/me/reconcile`
retries provisioning if Server 2 was temporarily unavailable during signup.

## Environment variables

```bash
HOST=0.0.0.0
PORT=8001
MODEL_NAME=Qwen3.5-0.8B
MODEL_PATH=/models/Qwen3.5-0.8B-Q4_K_M.gguf
INTERNAL_API_TOKEN=
AURA_SKIP_MODEL_LOAD=false
MAX_NEW_TOKENS=256
TEMPERATURE=0.7
TOP_P=0.95
LOAD_IN_4BIT=0
QUANTIZATION=
```

Set INTERNAL_API_TOKEN in a local .env file before starting the service. Do not commit any real secret values.

## Docker

```bash
cd ~/aura/server2
cp .env.example .env
# set INTERNAL_API_TOKEN in .env

docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100
```

The compose file keeps the model mount read-only and does not copy the model into the image.

## Health and inference endpoints

### GET /health

Returns API liveness without creating or loading a global/pre-user model
runtime. Before the first account is provisioned, `model_loaded` is `false`
and `active_brain_runtimes` is `0`. Each new local runtime is loaded only for
its owner after Server 1 provisions that Brain.

```json
{"status":"ok","provider":"local","model":"Qwen3.5-0.8B","model_loaded":false,"active_brain_runtimes":0}
```

Use the per-Brain status endpoints to check an individual user's runtime.

### POST /v1/brain/infer

Requires:

```http
Authorization: Bearer <INTERNAL_API_TOKEN>
```

Request example:

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "query": "Give me a brief answer.",
  "context": {
    "user_level": "general",
    "relevant_context": {"topic": "fitness"},
    "safety_constraints": []
  },
  "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
  "model": "Qwen3.5-0.8B"
}
```

Response example:

```json
{
  "model": "Qwen3.5-0.8B",
  "response": "...",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "request_id": "6ba7b810-9dad-11d8-80b4-00c04fd430c8",
  "status": "ok",
  "tokens_used": 42,
  "latency_ms": 1240
}
```

## Running on a small GPU (4-bit)

A 4B checkpoint in bfloat16 needs ~8 GiB of VRAM and will OOM on a 6 GiB GPU
such as an RTX 4050 laptop. Install the optional quantization deps and enable
4-bit loading:

```bash
pip install -r requirements.txt -r requirements-4bit.txt
LOAD_IN_4BIT=1 AURA_SKIP_MODEL_LOAD=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Each per-Brain runtime reports its quantization in Brain provisioning/status
metadata. `GET /health` remains a service-liveness check.
`QUANTIZATION=4bit|8bit|none`
overrides `LOAD_IN_4BIT`/`LOAD_IN_8BIT`. Quantization only takes effect on a
CUDA device; on CPU the flag is ignored and the model loads in full precision.

## CPU and RAM notes

Each local Brain has its own model weights, runtime and context, so provisioning
additional users increases disk and compute use (for example, a separate
564 MiB copy for every account using the 0.8B GGUF). The capacity manager limits
the number of live runtimes; if a dedicated runtime cannot start, that Brain
is marked unavailable rather than borrowing another user's runtime. The
deployment model file remains the source artifact and is never modified.

With Docker Compose, `BRAIN_RESOURCE_ROOT` defaults to `/var/data/brains`,
inside the persistent `server2_data` volume. Keep that volume when updating or
recreating the container; deleting the volume deletes Brain manifests,
private model copies, and storage data.
Set `BRAIN_MAX_CONCURRENT_RUNTIMES` conservatively for the laptop's measured
RAM/VRAM; each concurrently active Brain loads its own model copy and runtime.
Other users' private model files remain on disk when their runtime is not
active. If capacity is exhausted, provisioning/inference reports unavailable
instead of sharing a runtime.

## Security and isolation

- Server 2 does not authenticate end users or manage their accounts; Server 1 does.
- Internal endpoints require the server-to-server bearer token.
- Brain data APIs require the authenticated owner ID and Brain ID to match the Server 2 registry.
- Brain manifests contain identifiers and resource references only; credentials are rejected before persistence.

## Server 1 integration model

Server 1:

1. authenticate the end user
2. prepare and filter the context, including the caller's authorized app namespaces
3. send an authenticated request to Server 2
4. receive the generated answer

Server 2 validates the internal bearer token and Brain ownership, routes inference to that Brain's own runtime, and returns a generated answer.
The runtime may emit the explicit `AURA_TOOL_CALL:` JSON envelope. Server 2
executes only the fixed vault tools (`search`, `list_records`, `get_record`,
`create_record`, and `update_record`), rechecks Brain-scoped app grants, and
returns bounded results for the next model turn. Tool calls are limited to
four rounds of at most four calls each; arguments and execution time are
bounded. Stored content is treated as untrusted input.

In normal mode, read tools access namespaces authorized by Server 1 and
confirmed by Server 2's app-consent snapshot. With the owner-approved
`manage_vault: true` query context, tools can read and organize all namespaces
Server 1 lists for that owner's Brain, including inactive app connections.
This mode remains restricted to the resolved Brain and owner. Tool writes
still require `confirm_tool_writes: true`; normal-mode writes also require the
calling app's `memory:write` grant and stay in that app's namespace. In
management mode, records may be updated or derived records may be created in
any listed app namespace. Updates retain prior versions, AI-managed changes
are identified in record metadata, and the tool interface has no delete
operation. Tool audit rows contain Brain/owner, tool, request, status, and
safe error codes only; record content and tool arguments are not copied into
the audit log.

## Real inference testing

```bash
cd ~/aura/server2
.venv/bin/python -m pytest -q tests
```

The integration test loads the actual local GGUF checkpoint and verifies a real generation call against Qwen3.5-0.8B.

## Important constraints

- The configured local model file is a read-only source; a separate copy, runtime/context and user resources are provisioned per Brain.
- No additional model is downloaded.
- No replacement model is created.
- Server 1 remains responsible for user authentication and account management.
