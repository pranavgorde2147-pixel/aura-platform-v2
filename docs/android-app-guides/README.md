# AURA Android Studio Guide Index

The canonical product boundary is [AURA Product and Shared-Capability
Inventory](../AURA_APP_INVENTORY.md). This folder contains one Android Studio
handoff guide for each of the seven primary AURA apps and one separate
Chronos guide. Shared capabilities are documented in the app guide that uses
them; capabilities are not separate app guides.

## Shared Brain and Vault access rule

Every AURA app may use the shared Brain and other server capabilities that are
relevant to that app and supported by its API contracts. Brain query/search
may include read-only records from other active app connections with
`brain:read` (and Files documents with `files:read`); show their source and do
not treat them as app-owned records. Each app's Vault screen and its
edit/delete actions stay within that app's own namespace. AURA Core is the
exception: its explicit, user-controlled Whole-Vault Mode can manage all app
namespaces in that user's Brain, with destructive actions still confirmed.

For AI-assisted Vault changes, the `harness:` prefix does not add permissions:
the app's `memory:write` grant is required for writes and delete plans. The
fixed Harness supports search, list/read, exact duplicate detection, create,
update/rename, tags through record metadata, and confirmation-gated deletion.
Copy can create a derived record; move and merge must be treated as staged,
non-atomic create-then-delete workflows. Reminder scheduling, trash/restore,
and automated cleanup are not implemented server capabilities.

## Seven primary AURA applications

| Product | Guide |
| --- | --- |
| AURA Core | [AURA Core Android Studio guide](./AURA-CORE-GUIDE.md) |
| AURA Daily Diary | [Daily Diary Android Studio guide](./AURA-DIARY-GUIDE.md) |
| AURA Health | [Health Android Studio guide](./AURA-HEALTH-GUIDE.md) |
| AURA Study Companion | [Study Android Studio guide](./AURA-STUDY-GUIDE.md) |
| AURA Meeting AI | [Meeting Android Studio guide](./AURA-MEETING-GUIDE.md) |
| AURA Travel | [Travel Android Studio guide](./AURA-TRAVEL-GUIDE.md) |
| AURA Notification Hub | [Notifications Android Studio guide](./AURA-NOTIFICATIONS-GUIDE.md) |

These guides document backend contracts and integration requirements; they do
not prove the client application exists or is released. Check the catalog and
the canonical inventory for current status. Each guide includes a tailored,
ready-to-paste Android Studio prompt for a native implementation and its
app-specific UI, API, privacy, and verification requirements.

## Independent product

- [Chronos Android Studio guide](./AURA-CHRONOS-GUIDE.md) describes the
  repository's AURA-side catalog marker only. Chronos is an independent
  enterprise product, not an AURA app. No deployed Chronos integration is
  verified. Its prompt explicitly prevents inventing product/API behavior
  where requirements are not established.
