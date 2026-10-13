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
| `tasks` | AURA Tasks / Planner | planned | web | aura | — | memory:read, memory:write |
| `timeline` | AURA Timeline | module | internal | aura | — | timeline:read, timeline:write |
| `notifications` | AURA Notifications | planned | internal | aura | — | notifications:read |
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

## Backing models

- `InstalledModule` (`installed_modules`): user_id, module_slug, module_name,
  platform_type, is_active.
- `ModulePermission` (`module_permissions`): user_id, installed_module_id,
  scope, permission (`PermissionAction`).
