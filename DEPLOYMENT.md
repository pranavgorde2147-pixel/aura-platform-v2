# AURA Deployment Guide

Three services: **Server 1** (public API), **Server 2** (AI inference),
Server 3 (object storage). Clients only ever talk to Server 1; it calls the
other two with the shared internal token.

```
App ──HTTPS+JWT──► Server 1 ──Bearer INTERNAL_API_TOKEN──► Server 2 (AI)
                             └────────────────────────────► Server 3 (storage)
```

## Topology options

| Option | Server 1 | Server 2 | Server 3 |
|--------|----------|----------|----------|
| A. All-cloud free | Render | Render + Groq (no GPU) | Render + R2 |
| B. Hybrid (GPU laptop) | Render/VM | Friend's RTX laptop + tunnel | Render + R2 |
| C. Single VM | same VM via Docker | same VM via Docker | same VM via Docker |

---

## Option B - Server 2 on a friend's GPU laptop

### 1. Fit the model in VRAM

A 4B checkpoint in bfloat16 needs ~8 GB; an RTX 4050 laptop has 6 GB. Use
4-bit quantization:

```bash
cd server2
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt -r requirements-4bit.txt
```

Create `server2/.env`:

```env
INTERNAL_API_TOKEN=<same strong token as Server 1>
AURA_SKIP_MODEL_LOAD=0
MODEL_PATH=/absolute/path/to/Qwen3.5-4B
LOAD_IN_4BIT=1
MODEL_NAME=Qwen3.5-4B
```

Start it:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Check `GET http://127.0.0.1:8001/health` - it should report `provider: local`
and `quantization: 4bit`.

### 2. Expose it to Server 1 without port forwarding

**Cloudflare Tunnel (recommended, free, works behind CGNAT):**

```bash
cloudflared tunnel --url http://127.0.0.1:8001
```

Copy the printed `https://<random>.trycloudflare.com` URL. It changes on every
restart, so for a stable setup create a *named* tunnel against your own domain.

Alternatives: `ngrok http 8001`, or **Tailscale** (private - only works if
Server 1 runs on a host you control, not Render).

### 3. Point Server 1 at the tunnel

In Server 1's environment:

```env
AI_SERVER_URL=https://<random>.trycloudflare.com
INTERNAL_API_TOKEN=<same token>
AI_SERVER_MODEL=Qwen3.5-4B
```

### 4. Keep the laptop alive

- Power settings: lid close = do nothing, disable sleep/hibernate, stay plugged in.
- Autostart Server 2 + the tunnel on boot (Windows: `nssm`/Task Scheduler;
  Linux: `systemd` unit or Docker `restart: unless-stopped`).

### 5. Failover when the laptop is off

Enable the Server 1 external fallback so brain queries still work:

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

`render.yaml` at the repo root defines all three Render services. Create the
managed services first, then paste their URLs/tokens into the dashboard:

- Postgres: Neon → `POSTGRES_URL`
- Redis: Upstash → `REDIS_URL` (`rediss://...`)
- Qdrant: Qdrant Cloud → `QDRANT_URL`
- Object storage: Cloudflare R2 (Render free has no persistent disks) → set
  Server 3 `STORAGE_BACKEND=s3` plus `S3_ENDPOINT_URL`, `S3_BUCKET`,
  `S3_ACCESS_KEY`, `S3_SECRET_KEY`

Server 2 in the cloud runs in external-provider mode
(`AURA_SKIP_MODEL_LOAD=1`) with a Groq key; the 4B model needs a GPU.

For a single VM use Docker:

```bash
cd server1 && docker compose up -d
cd ../server2 && docker compose up -d
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
```

Add the app's origin to `CORS_ALLOWED_ORIGINS` on Server 1. Never ship
`INTERNAL_API_TOKEN`/`STORAGE_API_TOKEN` to clients - only the user JWT.
