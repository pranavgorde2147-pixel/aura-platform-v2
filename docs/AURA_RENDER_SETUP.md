# Render setup for AURA Core and the Server 2 Cloudflare Tunnel

This guide configures the existing Render Server 1 service to reach Server 2
on the laptop. It does not deploy Server 2 to Render and does not migrate
existing data. Server 1 continues to own accounts, authentication,
permissions, and the user-to-Brain mapping. The user's Brain, model runtime,
vault, and object storage remain on Server 2.

The repository's `render.yaml` now defines Server 1 only. If the existing
Render Blueprint still shows an `aura-server2` resource, review its proposed
changes before syncing: this workspace cannot inspect the live Render
dashboard, and a Blueprint sync may propose deleting a resource that is no
longer in the manifest. Verify Server 2 is running on the laptop through the
tunnel first; do not approve deletion of any resource you still need.

## 1. Configure the Cloudflare Tunnel

Run Server 2 on the laptop on port `8001` and configure a Cloudflare Tunnel
hostname to route to that service, for example:

```text
https://brain.example.com  ->  http://localhost:8001
```

The same hostname can serve both inference and storage because Server 2
provides both APIs. Confirm the public hostname routes to the combined Server 2
application, not `server2/app/storage_app.py` (that alternate entry point
doesn't provide Brain inference or provisioning).

If Cloudflare Access protects the hostname, create a service token for the
Render Server 1 service. The token's policy must permit the Server 2 hostname.
Keep the token ID and secret in Render environment variables only. Configure
the tunnel so requests reach the Server 2 service with their API paths intact:
`/v1/brain/infer`, `/v1/brains/provision`, `/v1/brains/{id}/vault/*`,
`/v1/objects/*`, and `/v1/storage/ready`.

## 2. Set the existing Render Server 1 environment

In Render, open the existing Server 1 web service, choose **Environment**, add
or update the following variables, then save and deploy:

| Variable | Value |
| --- | --- |
| `AI_SERVER_URL` | Tunnel origin only, e.g. `https://brain.example.com` (no path suffix) |
| `STORAGE_SERVER_URL` | Same tunnel origin if it routes to the combined Server 2 app |
| `AI_SERVER_MODEL` | Must equal Server 2 `MODEL_NAME`, e.g. `Qwen3.5-0.8B` |
| `INTERNAL_API_TOKEN` | The same strong random service token configured on Server 2 |
| `STORAGE_API_TOKEN` | Set equal to Server 2 `STORAGE_API_TOKEN`; if Server 2 leaves that blank, it falls back to `INTERNAL_API_TOKEN` |
| `AI_ACCESS_CLIENT_ID` | Cloudflare Access service-token client ID, only if Access is enabled |
| `AI_ACCESS_CLIENT_SECRET` | Matching Cloudflare Access service-token secret, only if Access is enabled |
| `BRAIN_REMOTE_PROVISIONING_ENABLED` | `true` |

Use the Cloudflare public `https://` origin. Do not use `localhost`, `127.0.0.1`,
a private LAN address, or a URL ending in an API route. Both server clients add
their own route paths. `AI_ACCESS_CLIENT_ID` and
`AI_ACCESS_CLIENT_SECRET` are also used by Server 1's storage client.

Keep the existing `POSTGRES_URL` on Render for user accounts, authentication,
permissions, and Brain routing metadata. Do not remove or repoint it as part of
this change. Do not put personal vault content in new Server 1 database
features; new app records, query turns, extracted document text, and memories
are routed to Server 2 by the current gateway flows.

Keep Render's `JWT_SECRET_KEY`, database credentials, and all service secrets
private. If a credential was included in a previously shared screenshot or
message, rotate it in its provider dashboard and update the relevant service
environment before relying on it.

## 3. Set Server 2 on the laptop

Configure the Server 2 environment (not Render) with:

```text
INTERNAL_API_TOKEN=<same service token as Render>
MODEL_NAME=Qwen3.5-0.8B
MODEL_PATH=/models/Qwen3.5-0.8B-Q4_K_M.gguf
BRAIN_RUNTIME_MODE=dedicated
BRAIN_RUNTIME_ADAPTER=auto
```

Mount the model file read-only at `/models` and retain the `server2_data`
volume. Per-account model copies and Brain resource directories, the vault
database, and objects are stored in that persistent volume. Do not remove the
volume when restarting or upgrading Server 2. There is no preloaded user Brain;
Server 2 provisions an owner's Brain when Server 1 calls it during signup or
reconciliation.

The local service must be running and the tunnel connected for signup-time
provisioning, queries, file operations, and vault writes. If the laptop or
tunnel is offline, Server 1 can't reach these operations; Android apps should
retain unsynchronized content locally and retry with the same idempotency ID.

## 4. Verify without touching production personal data

After deployment, use a test account and verify:

1. Server 1 health is healthy.
2. Server 2 `/health` and `/v1/storage/ready` are reachable through the tunnel.
3. Create a test AURA account while the laptop and tunnel are online. Check
   `GET /api/v1/brain/me` for a ready Brain and inspect the Server 2 resource
   directory created for that account.
4. Connect the Diary, Study, Files, and AURA Core apps and verify their
   permissions in `GET /api/v1/apps/me`.
5. Ingest a test Diary record, then query it from AURA Core using
   `POST /api/v1/brain/query` with `source_app:"aura-core"`.
6. Upload a test document and verify authorized document retrieval.
7. Confirm another test account cannot retrieve either record.

Do not use a real user's private data for the first integration check. Existing
records in Render's legacy tables are not automatically moved or deleted by
changing these environment variables; migration requires a separately backed
up and verified operation.