# AURA Deployment Guide

Two services: **Server 1** (public API + control plane) and **Server 2**
(AURA Brain: inference + object storage). Clients only ever talk to Server 1;
it calls Server 2 with the shared internal token.

```
App ──HTTPS+JWT──► Server 1 ──Bearer INTERNAL_API_TOKEN──► Server 2 /v1/brain/infer
                             └──Bearer STORAGE_API_TOKEN──► Server 2 /v1/objects
```

Server 3 is retired: the object-storage engine now runs inside Server 2, and
Server 1 points `STORAGE_SERVER_URL` at Server 2.

## Topology options

| Option | Server 1 | Server 2 |
|--------|----------|----------|
| A. All-cloud free | Render | Render + Groq (no GPU) + Render disk or R2 |
| B. Hybrid (GPU laptop) | Render/VM | Friend's RTX laptop (GGUF) + tunnel |
| C. Single VM | same VM via Docker | same VM via Docker |

---

## Option B - Server 2 on a friend's GPU laptop

### 1. Run the model on the GPU

The bundled model is `Qwen3.5-0.8B-Q4_K_M.gguf` (~590 MB). It runs on CPU as
well as GPU; on the RTX 4050, offload every layer to VRAM.

```bash
cd server2
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows

# CPU wheel (works everywhere):
pip install -r requirements.txt -r requirements-gguf.txt
# GPU wheel (CUDA, for llama-cpp-python):
#   CMAKE_ARGS="-DGGML_CUDA=on" FORCE_CMAKE=1 pip install -r requirements.txt -r requirements-gguf.txt
```

Create `server2/.env`:

```env
INTERNAL_API_TOKEN=<same strong token as Server 1>
AURA_SKIP_MODEL_LOAD=0
MODEL_PATH=/absolute/path/to/Qwen3.5-0.8B-Q4_K_M.gguf
MODEL_NAME=Qwen3.5-0.8B
GGUF_N_GPU_LAYERS=-1
GGUF_N_CTX=4096
REPEAT_PENALTY=1.2
MIN_P=0.05
STORAGE_BACKEND=local
STORAGE_DATA_DIR=./data
STORAGE_DATABASE_URL=sqlite+aiosqlite:///./data/storage.db
STORAGE_API_TOKEN=<same as Server 1 STORAGE_API_TOKEN>
```

Start it:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Check `GET http://127.0.0.1:8001/health` - it should report `provider: gguf`
and `model: Qwen3.5-0.8B`.

> Use the HF transformers checkpoint instead by pointing `MODEL_PATH` at the
> model directory (health reports `provider: local`). Small-VRAM 4-bit
> quantization needs `requirements-4bit.txt` (`LOAD_IN_4BIT=1` / `QUANTIZATION=4bit`).

### 2. Expose it to Server 1 without port forwarding

Server 1 runs on Render, so it can only originate **outbound** connections; it
cannot dial `127.0.0.1` or a `100.x` tailnet address on your laptop. The tunnel
must therefore be outbound-only from the laptop (no inbound port opened) and
reachable from Render as an HTTPS endpoint.

**Recommended: Cloudflare Tunnel + Cloudflare Access (service token).**
`cloudflared` dials out to Cloudflare, so it works behind CGNAT and opens no
inbound port. Cloudflare Access then requires a service token on every request,
so the hostname is not usable by anonymous callers and the origin stays private.

1. Create a *named* tunnel for the hostname (for example `brain.example.com`),
   pointing at `http://127.0.0.1:8001`, and run `cloudflared` on the laptop.
   A quick throwaway tunnel also works but changes URL on every restart:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8001
   ```
2. In Cloudflare Zero Trust, create a **service token** and an Access
   application/policy for the hostname that requires that token.
3. On Server 1 set `AI_ACCESS_CLIENT_ID` / `AI_ACCESS_CLIENT_SECRET` to the
   service token values. Server 1 sends them as
   `CF-Access-Client-Id` / `CF-Access-Client-Secret`, so Access admits the
   request before it reaches the laptop. `INTERNAL_API_TOKEN` remains the
   second, application-layer check enforced by Server 2.

> **Tailscale caveat (verified):** Render web services run in unprivileged
> containers without `NET_ADMIN`/TUN, so a Render service cannot join a tailnet
> like a normal node. Tailscale *is* usable on Render, but only via userspace
> networking (a `tailscaled --tun=userspace-networking` sidecar/proxy inside the
> Server 1 container) or a separate Render subnet-router service. That requires
> Dockerfile/startup changes and an auth key. Cloudflare Tunnel avoids all of
> that while still keeping Server 2's port closed.
>
> Do **not** expose port 8001 directly to the public internet and do not disable
> TLS verification or the token checks.

### 3. Point Server 1 at the tunnel

In Server 1's environment (Render dashboard secrets - never in the repo):

```env
AI_SERVER_URL=https://brain.example.com
STORAGE_SERVER_URL=https://brain.example.com   # only if storage lives on the laptop too
INTERNAL_API_TOKEN=<same strong token as Server 2>
AI_SERVER_MODEL=Qwen3.5-0.8B
AI_ACCESS_CLIENT_ID=<cloudflare access client id>
AI_ACCESS_CLIENT_SECRET=<cloudflare access client secret>
```

### 4. Keep the laptop alive

- Power settings: lid close = do nothing, disable sleep/hibernate, stay plugged in.
- Autostart Server 2 + the tunnel on boot (Windows: `nssm`/Task Scheduler;
  Linux: `systemd` unit or Docker `restart: unless-stopped`).

### 5. Failover when the laptop is off

Storage keeps working (it lives on Server 2 only while the laptop is up; for
full resilience run a cloud Server 2 as well and split the URLs). Enable the
Server 1 external fallback so brain queries still work:

```env
AI_FALLBACK_ENABLED=true
AI_FALLBACK_URL=https://api.groq.com/openai/v1
AI_FALLBACK_KEY=<groq api key>
AI_FALLBACK_MODEL=llama-3.1-8b-instant
```

When Server 2 is unreachable, Server 1 answers via this provider (source
`external_fallback`). Sensitive context keys (vault/password/secret/token/jwt)
are stripped before sending.

---

## Option A/C - cloud deployment

`render.yaml` at the repo root defines both Render services
(`aura-server1a-core`, `aura-server2`). Create the datastores first, then paste
their URLs/tokens into the dashboard secrets listed below.

- Postgres: Neon → `POSTGRES_URL`
- Redis: Upstash → `REDIS_URL` (`rediss://...`)
- Qdrant: Qdrant Cloud → `QDRANT_URL`
- Object storage: a Render disk at `/var/data` (bundled in `render.yaml`) or
  Cloudflare R2 → set Server 2 `STORAGE_BACKEND=s3` plus `S3_ENDPOINT_URL`,
  `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

Server 2 in the cloud runs in external-provider mode
(`AURA_SKIP_MODEL_LOAD=1`) with a Groq key; the local GGUF needs a GPU.

### Exact Render deploy steps

1. Push the code to the target repo (`aura-platform-v2`) and open Render →
   **New → Blueprint**, pointing at that repo. Render reads `render.yaml` and
   proposes `aura-server1a-core` and `aura-server2`. (If a service with the old
   name already exists, do not create a duplicate — rename or reuse it.)
2. Create Neon Postgres, Upstash Redis and Qdrant Cloud clusters; copy their
   connection strings.
3. Deploy `aura-server2` first. Set dashboard secrets:
   - `INTERNAL_API_TOKEN` = a strong random string
   - `STORAGE_API_TOKEN` = the same string (or a separate one)
   - `EXTERNAL_AI_KEY` = your Groq API key
   Wait for `GET /health` → `provider: external`.
4. Deploy `aura-server1a-core`. Set dashboard secrets:
   - `POSTGRES_URL`, `REDIS_URL`, `QDRANT_URL` from step 2
   - `JWT_SECRET_KEY` = a new strong random string
   - `INTERNAL_API_TOKEN`, `STORAGE_API_TOKEN` = the values from step 3
   - `AI_SERVER_URL` = the internal URL of `aura-server2` (Render dashboard →
     your server2 service → Connect → Internal URL)
   - `STORAGE_SERVER_URL` = the same internal URL
   - `CORS_ALLOWED_ORIGINS` = your frontend origin(s)
   Its start command runs `alembic upgrade head` before booting.
5. Verify: `GET https://<server1>/api/v1/health` returns `status: healthy` with
   `ai_server` and `storage_server` connected.

For a single VM use Docker:

```bash
cd server1 && docker compose up -d --build   # infra + Server 1 + Server 2
```

Expose only Server 1's port (8000) publicly, behind a TLS-terminating proxy.

---

## App connection

Your app talks to Server 1 only:

```
POST /api/v1/users          # register
POST /api/v1/auth/login     # -> access_token + refresh_token
POST /api/v1/brain/query    # Bearer <access_token>
POST /api/v1/files          # multipart upload, Bearer <access_token>
GET  /api/v1/apps           # app catalog (public)
POST /api/v1/apps/me        # authorize an app for this user
```

Add the app's origin to `CORS_ALLOWED_ORIGINS` on Server 1. Never ship
`INTERNAL_API_TOKEN`/`STORAGE_API_TOKEN` to clients - only the user JWT.
