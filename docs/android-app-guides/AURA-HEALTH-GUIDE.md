# AURA Health / Wellness App – Android Studio Integration Guide

**App Slug:** `health`  
**Category:** Mobile (future)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `notifications:read`  
**Ingestion Adapter:** `/api/v1/apps/health/ingest` → `normalize_health` (FACT, `health.wellness_event`). **User-consented only.**

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "health"}
```
Handle 201/409. Treat as opt-in; show consent UI before first ingest.

## 2) Ingest wellness event
```json
POST /api/v1/apps/health/ingest
Authorization: Bearer <access_token>
{
  "event": "run",
  "content": "Ran 5k in 28 minutes",
  "value": 5,
  "unit": "km",
  "occurred_at": "2026-10-21T07:00:00Z",
  "external_id": "health-run-001"
}
```
Requires one of `content|notes|summary|text|message|value`.

## 3) Query brain
```json
POST /api/v1/brain/query
{"query": "How active was I this week?", "source_app": "health", "use_memory": true}
```

## 4) Verification
- [ ] Consent collected before connect/ingest
- [ ] Auto-connect succeeds
- [ ] Ingest returns 201
- [ ] Only consented data sent
