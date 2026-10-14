# AURA Application Inventory

Source of truth: `server1/app/services/app_registry.py` (`CATALOG`). The registry
API is mounted at `/api/v1/apps` (public catalog) and `/api/v1/apps/me` (per-user
connections). Every connection is scoped to the authenticated user; clients never
talk to app databases directly.

## Status legend

- **live** — shipped and reachable through the AURA Core API today.
- **module** — a shared capability/collection rather than a standalone client.
- **planned** — designed, not yet implemented; has a catalog entry.
- **future** — possible integration pending product confirmation.

## Catalog

| Slug | Name | Status | Client | Platform | Repo path | Default permissions |
|------|------|--------|--------|----------|-----------|---------------------|
| `aura-core` | AURA Core | live | web | aura | `server1/` | identity:read |
| `brain` | Personal AI Brain | live | web | aura | `server1/app/api/v1/brain.py` | brain:read, memory:read, memory:write |
| `meeting` | AURA Meeting | live | mobile | aura | meetingapp (Android) | brain:read, memory:write, files:write |
| `memory` | Memory Vault | live | internal | aura | `server1/app/api/v1/brain.py` | memory:read, memory:write |
| `files` | AURA Documents / Files | live | internal | aura | `server1/app/api/v1/files.py` | files:read, files:write |
| `notes` | AURA Notes | planned | mobile | aura | — | memory:write, files:write |
| `study` | AURA Study | planned | mobile | aura | — | memory:write, timeline:write |
| `diary` | AURA Daily Diary | planned | mobile | aura | — | memory:write, timeline:write |
| `travel` | AURA Travel | planned | mobile | aura | — | memory:write, files:write |
| `tasks` | AURA Tasks / Planner | planned | web | aura | — | memory:read, memory:write |
| `timeline` | AURA Timeline | module | internal | aura | — | timeline:read, timeline:write |
| `notifications` | AURA Notifications | planned | internal | aura | — | notifications:read, notifications:write |
| `workflow` | AURA Workflow | planned | internal | aura | — | workflow:read, workflow:write |
| `search` | AURA Search | planned | internal | aura | — | memory:read, files:read |
| `knowledge-graph` | AURA Knowledge Graph | module | internal | aura | — | knowledge:read, knowledge:write |
| `chronos` | Chronos | module | internal | chronos | — | knowledge:read, timeline:write |
| `health` | AURA Health / Wellness | future | mobile | aura | — | memory:write, notifications:read |

## Notes

- **Chronos** is not a standalone service in this repo. It exists as a
  `PlatformType.CHRONOS` enum value and a Qdrant collection
  (`chronos_knowledge`); the catalog entry marks the platform family and its
  knowledge/timeline permissions.
- **Live** brain/memory/files rows map to the existing `/api/v1/brain*` and
  `/api/v1/files*` routes; the registry just records authorization intent.
- **Planned/weekend** entries have catalog metadata only; they own no routes yet.
- Permission scopes are free-form strings paired with `PermissionAction`
  (`read` / `write` / `delete` / `admin`). A connection installs the
  `default_permissions` list; callers may override them at install/update time.

## Ingestion integrations (implemented)

Apps with a concrete server-side ingestion integration send one event to
`POST /api/v1/apps/{slug}/ingest` (bearer-auth, user resolved server-side). The
route enforces the connection's active permission for the app's scope/action
before anything is written. See `server1/app/services/app_ingestion.py`.

| Slug | Adapter | Event type | Memory kind | Required permission |
|------|---------|-----------|-------------|---------------------|
| `brain` (AURA Chat) | `normalize_chat` | `chat.message` | interpretation | memory:write |
| `study` | `normalize_study` | `study.lecture_recorded` | fact | memory:write |
| `diary` | `normalize_diary` | `diary.entry` | interpretation | memory:write |
| `meeting` | `normalize_meeting` | `meeting.recorded` | fact | memory:write |
| `travel` | `normalize_travel` | `travel.trip` | fact | memory:write |
| `notifications` | `normalize_notification` | `notification.received` | (none) | notifications:write |

Apps without an adapter (catalog metadata only): `aura-core`, `memory`, `files`,
`notes`, `tasks`, `timeline`, `workflow`, `search`, `knowledge-graph`, `chronos`,
`health`. These remain registry entries; adding an adapter is the extension point.

Frontends: **none of these clients exist in this repository** (no Flutter/Dart,
Android, or web client code is present). Only the server-side ingestion
integration is implemented. See `docs/AURA_APP_INTEGRATIONS.md`.

## Backing models

- `InstalledModule` (`installed_modules`): user_id, module_slug, module_name,
  platform_type, is_active.
- `ModulePermission` (`module_permissions`): user_id, installed_module_id,
  scope, permission (`PermissionAction`).
