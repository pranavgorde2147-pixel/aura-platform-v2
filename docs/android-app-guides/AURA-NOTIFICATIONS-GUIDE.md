# AURA Notifications App – Android Studio Integration Guide

**App Slug:** `notifications`  
**Category:** Internal (planned)  
**Queries Brain:** `false` (does **not** query the brain)  
**Ingests:** `true`  
**Default Permissions:** `notifications:read`, `notifications:write`  
**Ingestion Adapter:** `/api/v1/apps/notifications/ingest` → `normalize_notification` (timeline + knowledge node **only**, **no brain memory** created).

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "notifications"}
```
No `brain:read` required/expected. Do **not** call `/brain/query` with `source_app="notifications"`.

## 2) Ingest notification event
```json
POST /api/v1/apps/notifications/ingest
Authorization: Bearer <access_token>
{
  "title": "Build Succeeded",
  "message": "server1 deployed successfully",
  "level": "info",
  "source": "CI",
  "external_id": "notif-build-123"
}
```
Requires `message|body|text|content|title`. Returns 201 with `memory_id: null`, duplicate tracking still works.

## 3) Verification
- [ ] Connected with only `notifications:*` scopes
- [ ] Ingest returns `memory_id: null`, timeline count increases
- [ ] No brain query attempts (or correctly skipped)
