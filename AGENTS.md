# AURA Platform - Agent Instructions

## Repository Structure

Two deployable services in this workspace:

| Directory | Purpose | Port | Entry |
|-----------|---------|------|-------|
| `server1/` | AURA Core: FastAPI API server + Personal Brain (Postgres, Redis, Qdrant) | 8000 | `app/main.py:create_app()` |
| `server2/` | AURA Brain: inference service (local Qwen3.5-0.8B GGUF or external provider) **and** object storage API | 8001 | `app/main.py` |

The object storage engine (formerly a separate "Server 3") now lives inside
`server2/`. `server2/app/storage_app.py` is an alternate storage-only ASGI
entrypoint (no model/torch load) used for running and integration-testing the
storage subsystem; production serves storage alongside the brain via `app.main`.

## Quick Start - Backend (Server 1)

```bash
cd server1
cp .env.example .env  # set real secrets
docker compose up -d  # starts Postgres, Redis, Qdrant, and Server 2 (brain + storage)
```

Docker healthcheck: `GET /api/v1/health`

## Quick Start - Server 2 (AURA Brain)

```bash
cd server2
cp .env.example .env  # set INTERNAL_API_TOKEN
docker compose up -d  # mounts ./models (GGUF) read-only; override with MODEL_HOST_PATH
```

Health: `GET /health` (returns healthy only after model loads or when
`AURA_SKIP_MODEL_LOAD=1`). Storage health: `GET /v1/storage/health`; readiness:
`GET /v1/storage/ready`.

All services join the external `aura-network` Docker network so the backend
resolves `aura-server2` by name.

## Running Tests

Server 1 and Server 2 tests share the Server 1 venv (the storage subsystem in
Server 2 has no torch dependency). Set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` to
bypass a broken system pytest plugin.

**Backend:**
```bash
cd server1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```
Includes integration tests that boot Server 2's real storage app
(`app.storage_app`) and a real embedded PostgreSQL (`pgserver`). The real Qwen
integration test only runs when the model checkpoint exists and
`AURA_RUN_INTEGRATION=1`.

**Server 2 (skip model load):**
```bash
cd server2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```

## Key Constraints

- **Server 2 model**: local GGUF Qwen3.5-0.8B (`Qwen3.5-0.8B-Q4_K_M.gguf`, needs
  `llama-cpp-python>=0.3.36` for the `qwen35` arch) or the Qwen3.5-4B transformers
  checkpoint. Mounted read-only from host, never copied into a Docker image. Path
  resolves from `MODEL_PATH`, then `/models/Qwen3.5-0.8B-Q4_K_M.gguf`, then
  `server2/models/` (no personal absolute paths).
- **AURA_SKIP_MODEL_LOAD**: Set to `1` to skip model loading (used in tests and on
  Render; falls back to external LLM or test response).
- **INTERNAL_API_TOKEN**: Required for Server 2 (`/v1/brain/infer`) and its storage
  API (`/v1/objects*`). Must match across services. Server 1 sends it as a bearer
  token and forwards the authorized user via `X-Aura-Owner-Id`.
- **Backend infrastructure**: Requires Postgres 16, Redis 7, Qdrant v1.10.1. All have healthchecks in docker-compose.yml.
- **Python 3.14**: Server 1 Dockerfile uses `python:3.14-slim`; Server 2 uses
  `python:3.12-slim`. Both require `python-multipart` for uploads.

## Architecture Notes

- Backend API is versioned under `/api/v1` (auth, users, brain, health, identity, files, apps).
- `POST/GET/DELETE /api/v1/files*` proxies to the storage API on Server 2
  (`STORAGE_SERVER_URL`) with the authenticated user as owner; clients never talk
  to storage directly.
- `/api/v1/apps*` exposes the shared AURA app catalog and per-user app connections
  (`InstalledModule` + `ModulePermission`); every connection is scoped to the
  authenticated user.
- Personal Brain memories live in `brain_memories` and are labelled `fact` vs `interpretation`; nothing is persisted unless the caller opts in (`persist_interaction`) or explicitly stores a memory (`/api/v1/brain/memory`).
- SQLAlchemy models in `backend/app/models/` use UUID primary keys and timestamped base.
- Alembic migrations in `backend/alembic/`. Run with `alembic upgrade head`.
- Server 2 validates the internal bearer token, not user auth. User auth is Server 1's responsibility.
- Server 2 prompt building strips sensitive context keys (vault, password, secret, token, jwt).
- When Server 2 is unreachable, Server 1 can answer via an external OpenAI-compatible
  provider if `AI_FALLBACK_ENABLED=true` (source `external_fallback`); sensitive
  context keys are stripped before forwarding too. See `DEPLOYMENT.md` for the
  GPU-laptop + tunnel + failover setup.

## Environment Variables

**Backend** (`server1/.env`):
- `POSTGRES_URL`, `REDIS_URL`, `QDRANT_URL`, `QDRANT_API_KEY` - infrastructure connections
- `JWT_SECRET_KEY` - change from default in production
- `AI_SERVER_URL` - defaults to `http://localhost:8001` (Server 2)
- `STORAGE_SERVER_URL` - defaults to `http://localhost:8001` (storage API on Server 2)
- `INTERNAL_API_TOKEN`, `STORAGE_API_TOKEN` - shared internal tokens
- `QDRANT_LOCAL_PATH` (on-disk Qdrant) and `USE_FAKE_REDIS` for local dev
- `AI_FALLBACK_ENABLED`, `AI_FALLBACK_URL`, `AI_FALLBACK_KEY`,
  `AI_FALLBACK_MODEL` - optional external provider used when Server 2 is down

**Server 2** (`server2/.env`):
- `INTERNAL_API_TOKEN` - must match Server 1's token
- `AURA_SKIP_MODEL_LOAD` - `true`/`1` to skip model loading
- `EXTERNAL_AI_URL`, `EXTERNAL_AI_KEY`, `EXTERNAL_MODEL` - for external LLM fallback
- `GGUF_N_GPU_LAYERS`, `GGUF_N_CTX`, `REPEAT_PENALTY`, `MIN_P` - GGUF runtime tuning
- `LOAD_IN_4BIT` / `QUANTIZATION` (`4bit`/`8bit`/`none`) - quantize the local
  checkpoint so it fits small-VRAM GPUs; needs `requirements-4bit.txt`
  (`bitsandbytes` + `accelerate`) and a CUDA device
- `STORAGE_BACKEND` (`local`/`s3`), `STORAGE_DATA_DIR`, `STORAGE_DATABASE_URL`,
  `STORAGE_API_TOKEN`, `MAX_UPLOAD_SIZE_BYTES`, `ALLOWED_CONTENT_TYPES` - object storage

## Environment Caveats (this workspace)

- No Docker daemon access (socket permission denied), no GPU, and no local
  4B checkpoint: real 4B local inference and `docker compose` cannot be executed
  here and must be reported as BLOCKED. The GGUF 0.8B model was verified on CPU.
- Tests disable pytest plugin autoload (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`)
  because a global ROS plugin breaks collection.
