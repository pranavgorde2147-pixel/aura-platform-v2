# Chronos — Android Studio Integration Guide for an Independent Product

> **Product boundary:** Chronos is a separate enterprise product. It is not an
> AURA app, Android application, AURA Core feature, Notification Hub feature,
> Timeline replacement, or AURA scheduling module. Do not count it in the
> seven-app AURA inventory.

Use this guide as a boundary and API reference when planning a separately
approved Chronos client integration. It does not authorize an integration or
describe a verified Chronos Android application.

This file is retained because it contains useful AURA-side API observations.
The current repository has an AURA catalog entry with slug `chronos`,
`platform_type="chronos"`, and grants `brain:read`, `knowledge:read`, and
`timeline:write`. Optional catalog grants include `memory:write`,
`chronos:delete`, and record permissions. These are implementation markers;
they do not determine Chronos product ownership or prove a live external
integration.

The repository search found no Chronos-owned product/client code or deployed
integration. The independent product's identity, login, tenant model, and
application lifecycle are therefore **unknown from this repository**.
Potential AURA service reuse must be explicitly approved, documented, and
implemented through authenticated, supported APIs. Never call Server 2
directly from a Chronos client.

## Optional AURA-side connection surface (not proof of integration)

If and only if an approved integration chooses to use AURA's per-user
connection model, the generic Server 1 catalog supports
`GET /api/v1/apps`, `GET /api/v1/apps/me`, and `POST /api/v1/apps/me` with
`{"module_slug":"chronos"}`. The request requires an AURA-authenticated user;
this does not define how enterprise Chronos authenticates its own tenants.
`PATCH /api/v1/apps/me/{connection_id}` replaces the full grant list when
`permissions` is supplied. `DELETE /api/v1/apps/me/{connection_id}` revokes
that AURA user connection. Do not treat this optional path as an existing
Chronos login or integration.

## Optional AURA Brain query surface

The generic gateway can authorize `source_app:"chronos"` when the
corresponding AURA connection is active and has `brain:read`. An explicitly
approved integration could then use `POST /api/v1/brain/query`:

```json
{
  "request_id":"<stable UUID>",
  "source_app":"chronos",
  "query":"<user's question>",
  "use_memory":true,
  "memory_limit":5,
  "context":{},
  "manage_vault":false,
  "confirm_tool_writes":false
}
```

This is only a supported AURA gateway possibility; no Chronos client or
integration using it is verified. If implemented later, reuse the ID only
for retrying that same query. The response shape includes
`answer`, `request_id`, `source`, `records_used`, `memories_used`, `error_code`,
`pending_delete_plans`, `delete_result`, `pending_bulk_replace_plans`, and
`bulk_replace_result`. Normal retrieval honors active connections and grants.
Do not send `manage_vault:true`; cross-app management belongs only to AURA
Core's separate, explicit whole-vault preference.

## Catalog and implementation limits

The registry has no Chronos-specific ingestion adapter, no `ingest_scope`,
no record types, and `routable:false`; therefore
`POST /api/v1/apps/chronos/ingest` has no adapter and returns `404`. Existing
catalog grants and generic APIs do not establish what Chronos should store or
whether it should share any data. Do not invent Chronos payload fields or
create an AURA app on its behalf.

If an approved, authenticated integration deliberately uses the generic
Brain harness, its caller namespace is limited to `apps/chronos`; writes
require applicable grants and destructive operations require the structured
confirmation flow. This is not a Chronos feature contract and grants no
whole-vault access. Do not enable it until product and security ownership are
defined.

## Audit findings and next decision

Repository references to Chronos are limited to the AURA-side catalog
description, `PlatformType.CHRONOS`, and historical `chronos_knowledge`
collection references. They are not evidence of the independent enterprise
product implementation. The catalog description currently calls Chronos an
“AURA platform family marker”; this wording conflicts with the product
boundary and should be corrected in a separate code/catalog change. Code
changes are outside this documentation-only audit.

Before implementing any integration, decide whether Chronos will consume an
AURA API, which identity/tenant mapping authorizes it, what Brain/Vault
capabilities are needed, and what isolation and revocation model applies.
Until those questions are answered, treat integration status as **unverified**.

## Android Studio implementation prompt

```text
Build or complete the native Android Chronos application using the confirmed
Chronos product requirements and this guide's strict product/API boundaries.
First inspect the actual Chronos project, its approved requirements, design
system, identity provider, tenant model, architecture, API contracts, and
tests. Reuse established patterns. If no product requirements or app project
are available, create only a native Kotlin/Jetpack Compose/Material 3
application foundation and clearly report the missing decisions; do not
invent Chronos workflows or present placeholder behavior as functional.

Design a polished, enterprise-quality, accessible native UI that implements
only features supported by confirmed Chronos requirements and real APIs.
Use loading, empty, offline, authorization, tenant-selection (only if the
confirmed identity model requires it), and error states. Keep Chronos an
independent product; do not brand it as an AURA app or assume shared accounts.

Do not connect to AURA or use the `chronos` catalog marker unless an approved
integration contract, tenant-to-user authorization model, required grants,
and revocation/isolation behavior are explicitly present in the project
requirements. If such approval exists, use only supported Server 1 APIs and
the user's authorized token; never call Server 2 directly. Do not call
`/api/v1/apps/chronos/ingest`, which has no adapter. Never enable
`manage_vault:true` or claim whole-vault access.

Protect enterprise data and credentials from logs. Add tests for the actual
confirmed flows, authorization, tenant isolation, and failure states. Run
available Android tests and debug build; report changed files, test results,
and any product/API decisions that blocked implementation.
```
