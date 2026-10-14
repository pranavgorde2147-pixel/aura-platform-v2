# AURA Backend (Server 1)

FastAPI backend for the AURA platform. It authenticates end users, owns the
Personal Brain, and is the only service clients talk to.

## Responsibilities

- User registration, login, JWT access/refresh tokens, logout.
- Personal Brain: AI identity, brain lifecycle, memories, timeline.
- File API that proxies to the storage API on Server 2 on behalf of the
  authenticated user. Clients never reach storage directly.
- Unified health/readiness across PostgreSQL, Redis, Qdrant, and Server 2
  (brain + storage).

## API surface (`/api/v1`)

| Group        | Endpoints                                                        |
| ------------ | ---------------------------------------------------------------- |
| Auth         | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me` |
| Users        | `POST /users`, `GET /users/{id}`, `GET /users/email/{email}`      |
| Brain        | `POST /brain/query`, `/brain/ingest`, `/brain/memory`, `GET /brain/memory`, `GET /brain/context` |
| Files        | `POST /files`, `GET /files`, `GET /files/{id}`, `GET /files/{id}/metadata`, `DELETE /files/{id}` |
| Identity     | `/identity/*`                                                     |
| Health       | `GET /health`, `/health/live`, `/ready`                           |

## Security model

- End users authenticate here; Server 2 trusts only the shared
  internal bearer token.
- The file API forwards the authenticated user id as `X-Aura-Owner-Id`; the
  storage API filters every read, list and delete by that owner.
- Brain memories are labelled `fact` vs `interpretation`. Nothing is persisted
  unless the caller opts in (`persist_interaction`) or explicitly stores a
  memory. Only `fact` memories are forwarded to models as approved context.

## Configuration

See `.env.example`. Key values: `POSTGRES_URL`, `REDIS_URL`, `QDRANT_URL`,
`QDRANT_API_KEY` (required for Qdrant Cloud), `JWT_SECRET_KEY`, `AI_SERVER_URL`
(Server 2), `STORAGE_SERVER_URL` (Server 2 storage API),
`INTERNAL_API_TOKEN`, `STORAGE_API_TOKEN`.

Embedded modes for local development/tests: `QDRANT_LOCAL_PATH` (on-disk
Qdrant) and `USE_FAKE_REDIS=1` (in-process fakeredis).

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements/base.txt
cp .env.example .env  # set real secrets
alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Or with Docker (starts Postgres, Redis, Qdrant and Server 2):

```bash
docker compose up -d
```

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```

Integration tests boot Server 2's real storage app
(`app.storage_app`) and a real embedded PostgreSQL (`pgserver`); no Docker is
required.
