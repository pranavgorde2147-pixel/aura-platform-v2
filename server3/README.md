# AURA Server 3 - Storage Engine

Internal object storage service for the AURA platform. It stores user files and
their metadata and is **only** reachable with the shared internal service token.

## Responsibilities

- Persist arbitrary user objects with SHA-256 integrity metadata.
- Enforce per-owner isolation: every read, list and delete is scoped to the
  owner id that Server 1 supplies after authenticating the end user.
- Support a local filesystem backend (default) and an S3/MinIO backend.
- Keep metadata in SQLite (default) or PostgreSQL.

## Security model

- All `/v1/objects*` endpoints require `Authorization: Bearer <internal token>`
  validated with a constant-time comparison.
- The owner id arrives in the `X-Aura-Owner-Id` header. An object id alone can
  never be used to access another user's data.
- Server 3 never authenticates end users; that is Server 1's responsibility.

## Run locally

```bash
cp .env.example .env  # set STORAGE_API_TOKEN
python -m venv .venv && .venv/bin/pip install -r requirements.txt
STORAGE_API_TOKEN=dev-token STORAGE_BACKEND=local .venv/bin/python -m uvicorn app.main:app --port 8002
```

## Run with Docker

```bash
cp .env.example .env  # set STORAGE_API_TOKEN
docker compose up -d
```

## Endpoints

| Method | Path                          | Purpose                        |
| ------ | ----------------------------- | ------------------------------ |
| GET    | `/health`                     | Liveness + backend readiness   |
| GET    | `/ready`                      | Readiness (backend + database) |
| POST   | `/v1/objects`                 | Upload an object               |
| GET    | `/v1/objects`                 | List owned objects             |
| GET    | `/v1/objects/{id}/metadata`   | Object metadata                |
| GET    | `/v1/objects/{id}`            | Download object bytes          |
| DELETE | `/v1/objects/{id}`            | Delete object + metadata       |

## Tests

```bash
.venv/bin/python -m pytest -q tests
```
