# AURA Android App Integration Guides

This folder contains **one guide per app** for Android Studio agents. Each guide is self-contained and tells the agent exactly how to:

1. Configure the app to connect to AURA Server 1 (Render or local)
2. Auto-connect the app to the user's account on first run / login
3. Verify the connection against the live catalog + permissions
4. Send data to the Personal Brain via ingestion
5. Query the Personal Brain safely (only if authorized)
6. Test and verify end-to-end

**Base URLs (fill in with your Render values):**

| Service | URL | Notes |
| --- | --- | --- |
| Server 1 (AURA Core) | `https://aura-server1a-core.onrender.com` | API host. All `/api/v1/*` endpoints live here. |
| Auth | `/api/v1/auth/login`, `/api/v1/auth/refresh`, `/api/v1/auth/me` | Bearer JWT access token required for protected endpoints |
| Apps / Connections | `/api/v1/apps`, `/api/v1/apps/me`, `/api/v1/apps/me/{id}` | Catalog is public, connections require auth |
| Brain Query | `/api/v1/brain/query` | Requires `Authorization` + active connection with `brain:read` if `source_app` is used |
| Brain Ingest (direct) | `/api/v1/brain/ingest` | Requires auth; enforces `source_app` permission if provided |
| App Ingest (routed) | `/api/v1/apps/{slug}/ingest` | **Preferred for app events**. Enforces connection + adapter scope/action server-side. |

**Auth model:** After login, send `Authorization: Bearer <access_token>` on every protected request. Never put tokens in query params.

**Connection model:** "Auto-connect" = call `POST /api/v1/apps/me` with `{"module_slug": "<APP_SLUG>"}` using the logged-in user's token. The server grants default permissions from the catalog. Idempotent for the same user+slug? If already connected, the API returns `409 CONFLICT` (`App already connected: ...`). Your client must handle 409 gracefully (treat as already connected).

**Important:** The fix for `brain:read` is in **server1**. If you're testing against Render and still see 403 on `/brain/query` with `source_app=<slug>`, ensure the deployed server1 includes the updated `app/services/app_registry.py` (which grants `brain:read` to apps with `queries_brain=true`).
