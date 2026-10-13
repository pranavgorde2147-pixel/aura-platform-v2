# AURA Server 2

AURA Server 2 is the actual Personal Brain runtime for the local Qwen3.5-4B checkpoint. This service is intentionally limited to internal inference and health endpoints; it does not implement a database, user authentication flow, memory store, vault access, or Server 3 integration.

## Purpose

Server 2 is the execution boundary for the Personal Brain. The brain is the Qwen3.5-4B model mounted from the host and loaded once at startup. Server 1 remains responsible for authenticated user context preparation and safe request forwarding.

## Model source of truth

The model is mounted from the host at:

- Host: ~/aura/server2/models/Qwen3.5-4B
- Container: /models/Qwen3.5-4B

The model is never copied into the Docker image. It remains read-only and is mounted via Docker Compose using the existing file mapping.

## Runtime behavior

- Server 2 authenticates the internal Server 1 bearer token.
- The request is validated for UUIDs, non-empty query, correct model name, and approved context.
- The service builds a deterministic prompt using approved context and safety constraints only.
- Qwen3.5-4B is loaded once at startup and reused for subsequent inference requests.
- The model runs on CPU unless CUDA is explicitly available.
- Docker health checks only report healthy after the application is ready.

## Environment variables

```bash
HOST=0.0.0.0
PORT=8001
MODEL_NAME=Qwen3.5-4B
MODEL_PATH=/models/Qwen3.5-4B
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

Returns:

```json
{"status":"ok","model":"Qwen3.5-4B"}
```

Only returns healthy after the model is actually loaded and ready.

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
  "model": "Qwen3.5-4B"
}
```

Response example:

```json
{
  "model": "Qwen3.5-4B",
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

`GET /health` then reports `quantization: 4bit`. `QUANTIZATION=4bit|8bit|none`
overrides `LOAD_IN_4BIT`/`LOAD_IN_8BIT`. Quantization only takes effect on a
CUDA device; on CPU the flag is ignored and the model loads in full precision.

## CPU and RAM notes

- Host resources are limited: approximately 14 GiB RAM and 4 GiB swap.
- The checkpoint is approximately 8.8 GiB on disk.
- The app loads the model once and reuses it.
- The runtime avoids unnecessary tensor retention and uses inference mode for generation.
- If loading fails for a real resource reason, the service returns the exact failure instead of switching models.

## Security and isolation

- This is an internal inference service, not an external application-facing API.
- There is no user database, vault access, or Server 3 dependency.
- There is no memory store or personal-data retrieval beyond the approved request context.
- Server 2 does not perform user authentication or user account management.

## Server 1 integration model

Later, Server 1 will:

1. authenticate the end user
2. prepare and filter the context
3. send an authenticated request to Server 2
4. receive the generated answer

Server 2 only validates the internal bearer token, validates the request, constructs a prompt, runs Qwen3.5-4B, and returns a generated answer.

## Real inference testing

```bash
cd ~/aura/server2
.venv/bin/python -m pytest -q tests
```

The integration test loads the actual local checkpoint and verifies a real generation call against Qwen3.5-4B.

## Important constraints

- Qwen3.5-4B is the only model used.
- No additional model is downloaded.
- No replacement model is created.
- No database or vault access is added.
- No Server 1 or Server 3 dependencies are introduced.
