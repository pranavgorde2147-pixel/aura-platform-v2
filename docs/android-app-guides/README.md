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

**Connection model:** After login, call `POST /api/v1/apps/me` with `{"module_slug": "<APP_SLUG>"}` using the logged-in user's token and omit `permissions` to receive the catalog defaults. A successful connection is `201`. This endpoint does not update an existing connection: it returns `409 CONFLICT` when that app is already connected. On `409`, fetch `GET /api/v1/apps/me`, locate the connection by `module_slug`, and inspect its `is_active` state and grants.

`PATCH /api/v1/apps/me/{connection_id}` updates an existing connection. When `permissions` is provided, it **replaces the entire permission list**; it does not add only the listed permission. Before updating, merge the required permission into the permissions already returned by `/apps/me`, preserving existing required grants. Then fetch `/apps/me` again and verify the final active connection has every required grant. Do not delete/reconnect a user's connection to change permissions.

For AURA Core, the required query grant is only `brain:read`. The current registry defaults to `brain:read`; do not request `memory:write` or `timeline:write` for chat. Preserve `identity:read` if the app's existing functionality needs it. Brain requests must use `source_app: "aura-core"`.

Handle response classes distinctly: `401` means authentication is missing or expired; `403` means the authenticated app connection lacks the required permission (not a network failure); `409` means the app is already connected and must be inspected/reconciled as above. Report actual transport failures and `5xx` responses separately from these API errors.

**Important:** The registry's default grant affects only new connections. It does not retroactively update existing user connections. For a production `403`, verify the deployed Server 1 catalog and the user's `/api/v1/apps/me` connection rather than assuming the connection inherited the new default.
