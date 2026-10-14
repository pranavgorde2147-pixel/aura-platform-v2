# AURA Platform

A two-service AI platform: a public backend with a persistent "Personal Brain"
(Server 1), and an internal AURA Brain service (Server 2) that provides LLM
inference and object storage.

This README is the single source of truth for connecting an app, deploying the
services, and changing the brain model.

---

## 1. Architecture

```
                    ┌────────────────────────────────────────────┐
   Your app  ──JWT─►│  Server 1  (public, port 8000)             │
 (web/mobile)       │  auth · users · brain · files · apps       │
                    └───────┬───────────────────────┬────────────┘
                            │ Bearer <INTERNAL_API_TOKEN>        │ Bearer <STORAGE_API_TOKEN>
                            ▼                                    ▼
                   ┌───────────────────────────────────────────────────────┐
                   │ Server 2 (8001)  AURA Brain                            │
                   │  /v1/brain/infer  inference                            │
                   │  /v1/objects      object storage                       │
                   └───────────────────────────────────────────────────────┘
                            │
                   Postgres · Redis · Qdrant (Server 1 infra)
```

Only **Server 1** is user-facing. Server 2 is internal and requires a shared
bearer token; your app never talks to it directly.

| Service | Directory | Port | Public? | Role |
|---------|-----------|------|---------|------|
| Server 1 | `server1/` | 8000 | Yes | Auth, users, Personal Brain, file API, app registry, health |
| Server 2 | `server2/` | 8001 | No | LLM inference (`/v1/brain/infer`) + object storage (`/v1/objects`) |

---

## 2. Quick start (local, Docker)

Docker Compose in `server1/` starts Postgres, Redis, Qdrant, Server 1 and
Server 2 (brain + storage).

```bash
# Server 1 + infra + Server 2 (brain and storage)
cd server1
cp .env.example .env            # set real secrets
docker compose up -d --build
docker compose exec backend alembic upgrade head
curl http://localhost:8000/api/v1/health
curl http://localhost:8001/health
```

To run Server 2 on a GPU host instead, start it on its own and mount the GGUF
model read-only:

```bash
cd server2
cp .env.example .env            # set INTERNAL_API_TOKEN (must match Server 1)
MODEL_HOST_PATH=/path/to/models docker compose up -d --build
```

> The `aura-network` external network is created by either compose file; start
> whichever service brings it up first.

### Local dev without Docker

```bash
cd server1
.venv/bin/uvicorn app.main:app --reload --port 8000
```
Set `USE_FAKE_REDIS=true` and `QDRANT_LOCAL_PATH=./data/qdrant` so Redis/Qdrant
are embedded; point `POSTGRES_URL` at any Postgres.

---

## 3. Health & readiness

| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `GET /api/v1/health` | Server 1 unified status | Always 200 |
| `GET /api/v1/health/live` | Liveness | `{"status":"alive"}` |
| `GET /api/v1/ready` | Readiness | 503 unless DB+Redis+Qdrant reachable |
| `GET /health` (S2) | Inference liveness | `provider: local\|gguf\|external` |
| `GET /ready` (S2) | Model loaded? | 503 until loaded |
| `GET /v1/storage/health` · `/v1/storage/ready` (S2) | Storage liveness / readiness | `/ready` enforces DB + backend |

Server 1 `/api/v1/health` shape:

```json
{
  "status": "healthy",              // healthy | degraded | unhealthy
  "core_ready": true,
  "services": {
    "database":       {"status": "connected"},
    "redis":          {"status": "connected"},
    "qdrant":         {"status": "connected"},
    "ai_server":      {"status": "connected", "model": "Qwen3.5-0.8B"},
    "storage_server": {"status": "connected", "backend": "local"}
  },
  "version": "1.0.0",
  "timestamp": "2026-01-01T00:00:00+00:00"
}
```
`core_ready` requires database + redis + qdrant. AI/storage being down degrades
the platform but does not block auth/user operations.

---

## 3. Security model

- **Users** authenticate only at Server 1 (JWT access + refresh tokens).
- Server 1 forwards the authorized user to the storage API on Server 2 as
  `X-Aura-Owner-Id` (a UUID). Server 2 filters every read/list/delete by that owner.
- Server 2 authenticates inference and storage via a shared internal bearer token
  (`INTERNAL_API_TOKEN` / `STORAGE_API_TOKEN`) compared with
  `hmac.compare_digest`.
- Server 2 strips sensitive context keys before building a prompt:
  `vault`, `password`, `secret`, `token`, `jwt`.
- **Never ship internal tokens to clients.** The app only ever holds the
  user's JWT.

Token lifetimes: access = 15 min (`ACCESS_TOKEN_EXPIRE_MINUTES`), refresh = 7 days
(`REFRESH_TOKEN_EXPIRE_DAYS`).

---

## 4. Server 1 API reference (prefix `/api/v1`)

Authentication: send `Authorization: Bearer <access_token>` on `(auth)` routes.

### Health
| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | – |
| GET | `/health/live` | – |
| GET | `/ready` | – |

### Auth
| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/auth/login` | `{email, password}` | → `{access_token, refresh_token, token_type, expires_in}` |
| POST | `/auth/refresh` | `{refresh_token}` | → new tokens |
| POST | `/auth/logout` | `{refresh_token}` | auth; 204 |
| GET | `/auth/me` | – | auth; `{user_id, email}` |

### Users
| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/users` | `{email, password, full_name}` | 201; creates user + identity + brain |
| GET | `/users/{user_id}` | – | auth; self only (403 otherwise) |
| GET | `/users/email/{email}` | – | auth; self only |

### Identity & Brain metadata
| Method | Path | Body/Query | Notes |
|--------|------|------------|-------|
| POST | `/identity?user_id=<uuid>` | `{display_name?}` | auth; self only |
| GET | `/identity/{identity_id}` | – | auth |
| POST | `/brain?ai_identity_id=<uuid>` | `{brain_name, version?}` | auth |
| GET | `/brain/{ai_identity_id}` | – | auth |
| GET | `/brain/{ai_identity_id}/context` | – | auth; aggregated context |

### Personal Brain (the user-facing brain)
| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/brain/query` | `{query, context?, use_memory?, persist_interaction?, memory_limit?}` | Calls Server 2 |
| POST | `/brain/ingest` | `{content, content_type?, source_app?, metadata?}` | Persists a `fact`; `source_app` requires an authorized app connection |
| POST | `/brain/memory` | `{content, kind?, summary?, source_app?, importance?}` | 201; explicit memory |
| GET | `/brain/memory?limit&offset` | – | list memories |
| GET | `/brain/context` | – | current user's brain context |

### App ingestion (concrete per-app integrations)

| Method | Path | Body | Notes |
|--------|------|------|-------|
| GET | `/apps` | – | public catalog |
| GET | `/apps/{slug}` | – | public catalog entry |
| GET | `/apps/me` | – | the user's app connections |
| POST | `/apps/me` | `{module_slug, permissions?}` | authorize an app (201) |
| PATCH/PUT | `/apps/me/{module_id}` | `{is_active?, permissions?}` | update a connection |
| DELETE | `/apps/me/{module_id}` | – | revoke a connection (204) |
| POST | `/apps/{slug}/ingest` | app-specific JSON | auth + active app permission required; routes one event into the user's brain (201) |

Supported ingestion slugs: `brain` (AURA Chat), `study`, `diary`, `meeting`,
`travel`, `notifications`. See `docs/AURA_APP_INTEGRATIONS.md`. Supplying an
`external_id` makes a write idempotent. No app frontends exist in this repo.

`/brain/query` response (`BrainQueryResponse`):
```json
{
  "answer": "...",
  "source": "server2_brain",
  "context_used": true,
  "user_id": "<uuid>",
  "model": "Qwen3.5-0.8B",
  "latency_ms": 1240,
  "tokens_used": 42,
  "memories_used": [{"id": "...", "kind": "fact", "source": "user", "content": "..."}],
  "persisted_memory_id": null
}
```
`source` values: `server2_brain` (normal), `external_fallback` (Server 2 down,
external provider used), `server2_error` (both failed).

Memories are labelled **`fact`** (user/app supplied) vs **`interpretation`**
(AI-generated) vs `event`. Nothing is persisted unless you opt in
(`persist_interaction=true` or `POST /brain/memory`).

### Files (Server 1 → Server 2 storage API proxy)
| Method | Path | Body | Notes |
|--------|------|------|-------|
| POST | `/files` | multipart `file`, `source_app?` | 201; 413 over limit |
| GET | `/files?limit&offset` | – | `{items, total, limit, offset}` |
| GET | `/files/{object_id}/metadata` | – | metadata |
| GET | `/files/{object_id}` | – | binary download |
| DELETE | `/files/{object_id}` | – | `{deleted, object_id}` |

Uploads larger than `MAX_UPLOAD_SIZE_BYTES` (default 50 MiB) return **413**;
storage failures return **503**.

### Apps (registry + per-user connections)
| Method | Path | Body | Notes |
|--------|------|------|-------|
| GET | `/apps` | – | public catalog (`docs/AURA_APP_INVENTORY.md`) |
| GET | `/apps/{slug}` | – | public; 404 if unknown |
| GET | `/apps/me` | – | auth; user's connected apps |
| POST | `/apps/me` | `{module_slug, permissions?}` | auth; 201; 409 if already connected |
| GET | `/apps/me/{module_id}` | – | auth; self only |
| PATCH/PUT | `/apps/me/{module_id}` | `{is_active?, permissions?}` | auth; update/replace |
| DELETE | `/apps/me/{module_id}` | – | auth; 204 |

Connections use `installed_modules` + `module_permissions` and are always scoped
to the authenticated user.

---

## 5. Internal API reference

### Server 2 (`/v1/brain/infer`) — bearer `INTERNAL_API_TOKEN`
Request:
```json
{
  "user_id": "<uuid>",
  "query": "…",
  "context": {"user_level": "general", "relevant_context": {}, "safety_constraints": []},
  "request_id": "<uuid, optional>",
  "model": "Qwen3.5-0.8B"
}
```
Response: `{model, response, user_id, request_id, status, tokens_used, latency_ms}`.
`model` must match Server 2's configured model (or its `EXTERNAL_MODEL` in
external mode), otherwise **400**.
Also: `GET /health`, `GET /ready`.

### Server 2 storage API (`/v1/objects`) — bearer `STORAGE_API_TOKEN`, `X-Aura-Owner-Id: <uuid>`
| Method | Path | Notes |
|--------|------|-------|
| POST | `/v1/objects` | multipart `file`, optional `ai_identity_id`, `source_app` → 201 |
| GET | `/v1/objects?limit&offset` | list for owner |
| GET | `/v1/objects/{id}/metadata` | metadata |
| GET | `/v1/objects/{id}` | download (adds `X-Object-Checksum-Sha256`) |
| DELETE | `/v1/objects/{id}` | delete |
Also: `GET /v1/storage/health`, `GET /v1/storage/ready`.

`ObjectMetadata`: `{id, owner_id, ai_identity_id, filename, content_type,
size_bytes, checksum_sha256, storage_backend, source_app, created_at, expires_at}`.

---

## 6. Data model (Postgres, Server 1)

Alembic migrations in `server1/alembic/`; run `alembic upgrade head`.

| Table | Purpose |
|-------|---------|
| `users` | accounts (UUID pk, email, password_hash, status) |
| `sessions` | refresh/session tokens |
| `ai_identities` | one AI identity per user |
| `personal_ai_brains` | one brain per identity (name, version, state) |
| `brain_memories` | persisted memories: `kind` = fact/interpretation/event, `source` = user/brain/app |
| `memory_vault` | references to stored objects |
| `knowledge_graph` | graph nodes/edges |
| `timeline` | activity events (e.g. `file.uploaded`) |
| `devices`, `installed_modules`, `module_permissions` | platform extensions |

Server 2's storage API owns its own metadata table for stored objects (SQLite by
default, or Postgres via `STORAGE_DATABASE_URL`).

---

## 7. Environment variables

### Server 1 (`server1/.env`)
| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_URL` | `postgresql+psycopg://aura:aura@postgres:5432/aura` | DB (app converts to asyncpg) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant |
| `QDRANT_LOCAL_PATH` | – | embedded on-disk Qdrant (local dev) |
| `USE_FAKE_REDIS` | `false` | in-process Redis (local dev/tests) |
| `JWT_SECRET_KEY` | dev default | **change in production** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | access token TTL |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | refresh token TTL |
| `AI_SERVER_URL` | `http://localhost:8001` | Server 2 base URL (use `http://aura-server2:8001` in Docker) |
| `AI_SERVER_MODEL` | `Qwen3.5-0.8B` | model id sent to Server 2 |
| `INTERNAL_API_TOKEN` | – | shared with Server 2 |
| `STORAGE_SERVER_URL` | `http://localhost:8001` | Server 2 storage base URL (use `http://aura-server2:8001` in Docker) |
| `STORAGE_API_TOKEN` | – | shared with Server 2 storage (falls back to `INTERNAL_API_TOKEN`) |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:5173` | comma-separated or JSON array |
| `MAX_UPLOAD_SIZE_BYTES` | `52428800` | upload limit |
| `AI_FALLBACK_ENABLED` | `false` | answer via external provider when Server 2 is down |
| `AI_FALLBACK_URL` | – | OpenAI-compatible base (e.g. `https://api.groq.com/openai/v1`) |
| `AI_FALLBACK_KEY` | – | fallback API key |
| `AI_FALLBACK_MODEL` | `llama-3.1-8b-instant` | fallback model |

### Server 2 (`server2/.env`)
| Variable | Default | Purpose |
|----------|---------|---------|
| `INTERNAL_API_TOKEN` | – | **must match Server 1** |
| `MODEL_NAME` | `Qwen3.5-0.8B` | model id (must match `AI_SERVER_MODEL`) |
| `MODEL_PATH` | `/models/Qwen3.5-0.8B-Q4_K_M.gguf` | local checkpoint (GGUF or HF dir) |
| `AURA_SKIP_MODEL_LOAD` | `false` | `1` = don't load local model (external/test mode) |
| `EXTERNAL_AI_URL` | – | OpenAI-compatible base (Groq) |
| `EXTERNAL_AI_KEY` | – | Groq key (or `GROQ_API_KEY`) |
| `EXTERNAL_MODEL` | `llama-3.1-8b-instant` | external model id |
| `MAX_NEW_TOKENS` / `TEMPERATURE` / `TOP_P` | `512`/`0.7`/`0.9` | generation params |
| `GGUF_N_GPU_LAYERS` / `GGUF_N_CTX` | `-1`/`4096` | GGUF offload layers / context |
| `REPEAT_PENALTY` / `MIN_P` | `1.2`/`0.05` | GGUF sampling (resolves echo loops) |
| `LOAD_IN_4BIT` / `LOAD_IN_8BIT` | `0` | quantize the local model (small GPUs) |
| `QUANTIZATION` | – | `4bit`/`8bit`/`none`; overrides the flags |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `STORAGE_DATA_DIR` / `STORAGE_LOCAL_ROOT` | `./data` / `./data/objects` | storage roots |
| `STORAGE_DATABASE_URL` | SQLite under data dir | storage metadata DB (Postgres supported) |
| `STORAGE_API_TOKEN` | – | **must match Server 1's `STORAGE_API_TOKEN`** |
| `MAX_UPLOAD_SIZE_BYTES` / `ALLOWED_CONTENT_TYPES` | `52428800` / empty | upload limit and allow-list |
| `S3_ENDPOINT_URL` / `S3_BUCKET` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_REGION` / `S3_USE_SSL` | – | S3/MinIO/R2 settings |

---

## 8. Connect an app

Your app talks to **Server 1 only** over HTTPS. Full flow:

1. Register: `POST /api/v1/users`
2. Login: `POST /api/v1/auth/login` → store `refresh_token` securely
3. Call APIs with `Authorization: Bearer <access_token>`
4. Refresh on 401: `POST /api/v1/auth/refresh`

Add your app's origin to `CORS_ALLOWED_ORIGINS` on Server 1.

### JavaScript client
```js
const BASE = "https://your-server1-host/api/v1";
let access, refresh;

export async function register(email, password, fullName) {
  const r = await fetch(`${BASE}/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, full_name: fullName }),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function login(email, password) {
  const r = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) throw new Error("invalid credentials");
  const t = await r.json();
  access = t.access_token; refresh = t.refresh_token;   // persist refresh securely
}

const authHeaders = () => ({ Authorization: `Bearer ${access}` });

export const askBrain = (query, useMemory = false) =>
  fetch(`${BASE}/brain/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ query, context: { user_level: "general" }, use_memory: useMemory }),
  }).then((r) => r.json());

export function uploadFile(file, sourceApp = "app") {
  const fd = new FormData();
  fd.append("file", file);
  if (sourceApp) fd.append("source_app", sourceApp);
  return fetch(`${BASE}/files`, { method: "POST", headers: authHeaders(), body: fd }).then((r) => r.json());
}

export const listFiles = () =>
  fetch(`${BASE}/files`, { headers: authHeaders() }).then((r) => r.json());

export const downloadFile = (id) =>
  fetch(`${BASE}/files/${id}`, { headers: authHeaders() }).then((r) => r.blob());
```

### cURL smoke test
```bash
BASE=http://localhost:8000/api/v1
curl -s -X POST $BASE/users -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"password123","full_name":"Me"}'

TOKEN=$(curl -s $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"password123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s $BASE/brain/query -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"Say hello","context":{"user_level":"general"}}'

curl -s $BASE/files -H "Authorization: Bearer $TOKEN" -F file=@./notes.txt -F source_app=myapp
```

---

## 9. Deployment

See `DEPLOYMENT.md` for full detail. Summary:

### A. Single VM (Docker) — all services
```bash
cd server1 && docker compose up -d --build   # infra + Server 1 + Server 2
```
Expose only 8000; terminate TLS with Caddy/nginx in front.
Docker-internal URLs are `AI_SERVER_URL=http://aura-server2:8001` and
`STORAGE_SERVER_URL=http://aura-server2:8001`.

### B. Render (managed PaaS)
`render.yaml` defines two services: `aura-server1a-core` and `aura-server2`.
Create managed datastores and paste their URLs as dashboard secrets:
- Postgres → **Neon** (`POSTGRES_URL`)
- Redis → **Upstash** (`REDIS_URL`)
- Qdrant → **Qdrant Cloud** (`QDRANT_URL`)

Server 1's deploy command runs `alembic upgrade head` automatically. Server 2
runs in external-provider mode (`AURA_SKIP_MODEL_LOAD=1`, Groq) with a Render
disk at `/var/data` for object storage. If you prefer not to use a disk, set
`STORAGE_BACKEND=s3` and the `S3_*` variables to use Cloudflare R2.

### C. Server 2 on a laptop GPU + cloud Server 1
```bash
# on the GPU laptop (GGUF Qwen3.5-0.8B runs on a 6 GB RTX 4050)
pip install -r server2/requirements.txt -r server2/requirements-gguf.txt
MODEL_PATH=/path/to/Qwen3.5-0.8B-Q4_K_M.gguf AURA_SKIP_MODEL_LOAD=0 \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
cloudflared tunnel --url http://127.0.0.1:8001     # public https URL, no port-forward needed
```
Point Server 1 at the tunnel: `AI_SERVER_URL=https://<tunnel>` and
`STORAGE_SERVER_URL=https://<tunnel>`, then enable `AI_FALLBACK_ENABLED=true` so
queries survive when the laptop sleeps. Keep the laptop awake (disable sleep,
lid-close = do nothing) and autostart the tunnel + uvicorn on boot.

---

## 10. Changing the brain model

Server 2 is the only place a model is defined. There are three ways to change it.

### Option 1 — swap the local checkpoint
1. Download the new model (a HuggingFace `transformers` checkpoint with
   `config.json` + safetensors) or a quantized `*.gguf` file.
2. Point Server 2 at it:
   ```env
   MODEL_NAME=<new-model-id>
   MODEL_PATH=/models/<new-model-dir-or-file.gguf>
   AURA_SKIP_MODEL_LOAD=false
   ```
3. Update Server 1 so the ids match:
   ```env
   AI_SERVER_MODEL=<new-model-id>
   ```
4. Restart Server 2, then Server 1. Verify: `GET http://server2:8001/health`
   shows `provider: local` (transformers) or `provider: gguf` (GGUF file).

The model id in the `/v1/brain/infer` request must match exactly or Server 2
returns **400**.

> GGUF: requires `llama-cpp-python>=0.3.36` (install `requirements-gguf.txt`).
> The bundled Qwen3.5-0.8B Q4_K_M checkpoint uses the `qwen35` architecture,
> which older llama-cpp-python builds cannot load. Tune with `GGUF_N_GPU_LAYERS`,
> `GGUF_N_CTX`, `REPEAT_PENALTY` and `MIN_P`.

### Option 2 — run a non-Qwen model via an external provider
```env
AURA_SKIP_MODEL_LOAD=1
EXTERNAL_AI_URL=https://api.groq.com/openai/v1
EXTERNAL_AI_KEY=<key>
EXTERNAL_MODEL=<provider-model-id>
```
Server 1's `AI_SERVER_MODEL` may be either the local `MODEL_NAME` or
`EXTERNAL_MODEL` in this mode.

### Option 3 — small-VRAM GPU (4-bit)
Install extras and quantize (fits a 4B model in ~3 GB VRAM):
```bash
pip install -r server2/requirements-4bit.txt   # bitsandbytes + accelerate
```
```env
AURA_SKIP_MODEL_LOAD=false
LOAD_IN_4BIT=1          # or QUANTIZATION=4bit|8bit|none
```
Quantization requires CUDA; on CPU the flag is ignored (full precision).

> Compatibility: keep `MODEL_NAME`/`AI_SERVER_MODEL` and the prompt template in
> `server2/app/main.py:_build_prompt` in mind. The service is model-agnostic as
> long as the checkpoint loads with `AutoModelForCausalLM`/`AutoTokenizer`.

---

## 11. Testing

```bash
# Server 1 (includes real PostgreSQL via pgserver + real storage subprocess)
cd server1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests

# Server 2 (skip model load)
cd server2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` avoids a broken system plugin (ROS). The real
Qwen inference test only runs with `AURA_RUN_INTEGRATION=1` and the checkpoint
present.

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `/ready` → 503 | Postgres/Redis/Qdrant unreachable; check `services` in `/health` |
| `brain/query` → `source: server2_error` | Server 2 down or token mismatch; enable `AI_FALLBACK_ENABLED` |
| Server 2 → 401 | `INTERNAL_API_TOKEN` differs between Server 1 and 2 |
| Storage → 401/503 | `STORAGE_API_TOKEN` mismatch; 503 means Server 2 storage has no token set |
| `brain/query` → 400 | `AI_SERVER_MODEL` ≠ Server 2's model id |
| File upload → 413 | Exceeds `MAX_UPLOAD_SIZE_BYTES` |
| File upload → 415 | Content type not in `ALLOWED_CONTENT_TYPES` (Server 2 storage) |
| Config crash on startup | `CORS_ALLOWED_ORIGINS` malformed (use comma or JSON array) |
| Disk full / no persistence | Render disk missing — mount a disk at the storage data dir or use `STORAGE_BACKEND=s3` (R2) |

---

## 13. Repository layout

```
.
├── README.md            # this file
├── DEPLOYMENT.md        # deployment details
├── AGENTS.md            # contributor/agent instructions
├── render.yaml          # Render blueprint (2 services)
├── docs/
│   └── AURA_APP_INVENTORY.md   # app catalog and connection permissions
├── server1/             # Server 1: FastAPI backend + brain + file API + app registry
└── server2/             # Server 2: inference (GGUF/transformers/external) + storage
```

> Not-yet-verified in a Docker/GPU environment: `docker compose` orchestration
> and 4B local inference (no Docker/GPU/4B checkpoint in the dev workspace). The
> GGUF Qwen3.5-0.8B model *was* verified on CPU, and all logic is covered by
> unit + live integration tests on real Postgres and the real storage engine.
