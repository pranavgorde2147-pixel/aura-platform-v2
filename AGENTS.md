# AURA Platform - Agent Instructions

## Repository Structure

Three independent services in this workspace:

| Directory | Purpose | Port | Entry |
|-----------|---------|------|-------|
| `server1/` | FastAPI API server + Personal Brain (Postgres, Redis, Qdrant) | 8000 | `app/main.py:create_app()` |
| `server2/` | Internal inference service (Qwen3.5-4B) | 8001 | `app/main.py` |
| `server3/` | Internal object storage engine | 8002 | `app/main.py:create_app()` |

## Quick Start - Backend

```bash
cd server1
cp .env.example .env  # set real secrets
docker compose up -d  # starts Postgres, Redis, Qdrant, and Server 3
```

Docker healthcheck: `GET /api/v1/health`

## Quick Start - Server 2

```bash
cd server2
cp .env.example .env  # set INTERNAL_API_TOKEN
docker compose up -d  # mounts ./models/Qwen3.5-4B read-only (override with MODEL_HOST_PATH)
```

Health: `GET /health` (returns healthy only after model loads)

## Quick Start - Server 3

```bash
cd server3
cp .env.example .env  # set STORAGE_API_TOKEN (must match Server 1's)
docker compose up -d
```

Health: `GET /health`; readiness: `GET /ready`

All three services join the external `aura-network` Docker network so the
backend resolves `aura-server2` and `aura-server3` by name.

## Running Tests

Backend and Server 3 tests share the backend venv. Set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` to bypass a broken system pytest plugin.

**Backend:**
```bash
cd server1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```
Includes integration tests that boot a real Server 3 process and a real embedded
PostgreSQL (`pgserver`). The real Qwen integration test only runs when the model
checkpoint exists and `AURA_RUN_INTEGRATION=1`.

**Server 3:**
```bash
cd server3
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ../server1/.venv/bin/python -m pytest -q tests
```

**Server 2 (skip model load):**
```bash
cd server2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```

## Key Constraints

- **Server 2 model**: Qwen3.5-4B only. Mounted read-only from host, never copied into Docker image. The model path is resolved from `MODEL_PATH`, then `/models/Qwen3.5-4B`, then `server2/models/Qwen3.5-4B` (no personal absolute paths).
- **AURA_SKIP_MODEL_LOAD**: Set to `1` to skip model loading (used in tests, falls back to external LLM or test response).
- **INTERNAL_API_TOKEN**: Required for Server 2 (`/v1/brain/infer`) and Server 3 (`/v1/objects*`). Must match across services. Server 1 sends it as a bearer token and forwards the authorized user via `X-Aura-Owner-Id`.
- **Backend infrastructure**: Requires Postgres 16, Redis 7, Qdrant v1.10.1. All have healthchecks in docker-compose.yml.
- **Python 3.14**: Backend Dockerfile uses `python:3.14-slim`. Server 1/3 both require `python-multipart` for uploads.

## Architecture Notes

- Backend API is versioned under `/api/v1` (auth, users, brain, health, identity, files).
- `POST/GET/DELETE /api/v1/files*` proxies to Server 3 with the authenticated user as owner; clients never talk to Server 3 directly.
- Personal Brain memories live in `brain_memories` and are labelled `fact` vs `interpretation`; nothing is persisted unless the caller opts in (`persist_interaction`) or explicitly stores a memory (`/api/v1/brain/memory`).
- SQLAlchemy models in `backend/app/models/` use UUID primary keys and timestamped base.
- Alembic migrations in `backend/alembic/`. Run with `alembic upgrade head`.
- Server 2 and Server 3 validate the internal bearer token, not user auth. User auth is Server 1's responsibility.
- Server 2 prompt building strips sensitive context keys (vault, password, secret, token, jwt).
- When Server 2 is unreachable, Server 1 can answer via an external OpenAI-compatible
  provider if `AI_FALLBACK_ENABLED=true` (source `external_fallback`); sensitive
  context keys are stripped before forwarding too. See `DEPLOYMENT.md` for the
  GPU-laptop + tunnel + failover setup.

## Environment Variables

**Backend** (`server1/.env`):
- `POSTGRES_URL`, `REDIS_URL`, `QDRANT_URL` - infrastructure connections
- `JWT_SECRET_KEY` - change from default in production
- `AI_SERVER_URL` - defaults to `http://localhost:8001` (Server 2)
- `STORAGE_SERVER_URL` - defaults to `http://localhost:8002` (Server 3)
- `INTERNAL_API_TOKEN`, `STORAGE_API_TOKEN` - shared internal tokens
- `QDRANT_LOCAL_PATH` (on-disk Qdrant) and `USE_FAKE_REDIS` for local dev
- `AI_FALLBACK_ENABLED`, `AI_FALLBACK_URL`, `AI_FALLBACK_KEY`,
  `AI_FALLBACK_MODEL` - optional external provider used when Server 2 is down

**Server 2** (`server2/.env`):
- `INTERNAL_API_TOKEN` - must match Server 1's token
- `AURA_SKIP_MODEL_LOAD` - `true`/`1` to skip model loading
- `EXTERNAL_AI_URL`, `EXTERNAL_AI_KEY`, `EXTERNAL_MODEL` - for external LLM fallback
- `LOAD_IN_4BIT` / `QUANTIZATION` (`4bit`/`8bit`/`none`) - quantize the local
  checkpoint so it fits small-VRAM GPUs; needs `requirements-4bit.txt`
  (`bitsandbytes` + `accelerate`) and a CUDA device

**Server 3** (`server3/.env`):
- `STORAGE_API_TOKEN` - must match Server 1's `STORAGE_API_TOKEN`
- `STORAGE_BACKEND` - `local` (default) or `s3`
- `STORAGE_DATABASE_URL` - defaults to SQLite under `STORAGE_DATA_DIR`; PostgreSQL supported
- `MAX_UPLOAD_SIZE_BYTES`, `ALLOWED_CONTENT_TYPES` - limits and allow-list

## Environment Caveats (this workspace)

- No Docker, no GPU, and no local Qwen checkpoint: real local inference and
  `docker compose` cannot be executed here and must be reported as BLOCKED.
- Tests disable pytest plugin autoload (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`)
  because a global ROS plugin breaks collection.
