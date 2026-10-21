# AURA Study App – Android Studio Integration Guide

**App Slug:** `study`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions (granted on connect):** `brain:read`, `memory:write`, `timeline:write`  
**Ingestion Adapter:** `/api/v1/apps/study/ingest` → `normalize_study` (stores as `MemoryKind.FACT`, `event_type=study.lecture_recorded`)

## 1) Endpoints
| Action | Method | Path | Auth |
| --- | --- | --- | --- |
| Catalog (verify app exists) | GET | `/api/v1/apps/study` | No |
| Connect app | POST | `/api/v1/apps/me` | Bearer token |
| List my connections | GET | `/api/v1/apps/me` | Bearer token |
| Ingest lecture/study record | POST | `/api/v1/apps/study/ingest` | Bearer token (connection required) |
| Query brain (optional) | POST | `/api/v1/brain/query` | Bearer token (connection with `brain:read` required if `source_app="study"`) |

## 2) Configuration
Create `app/src/main/res/xml/network_security_config.xml` or use HTTPS. Store base URL in `BuildConfig` or `local.properties`:

```kotlin
object AuraConfig {
  const val BASE_URL = "https://aura-server1a-core.onrender.com"
}
```

## 3) Auto-connect flow (implement this)
Do this **after successful login** (when you have `access_token`):

```kotlin
// 1) Ensure app exists in catalog (optional but good for validation)
GET $BASE_URL/api/v1/apps/study

// 2) Try to connect
POST $BASE_URL/api/v1/apps/me
Headers: Authorization: Bearer $accessToken, Content-Type: application/json
Body: {"module_slug": "study"}

// 3) Handle response
201 Created -> connected (save connection_id + permissions in prefs/Room)
409 Conflict -> already connected (fetch /apps/me and cache)
404 Not Found -> app not in catalog (log error, do not retry)
```

Cache `is_active`, `permissions[]` (must include `brain:read` for queries).

## 4) Ingest data (store lecture notes)
**Preferred endpoint:** `POST /api/v1/apps/study/ingest`

```json
{
  "notes": "Lecture 3 - Normalization (1NF, 2NF, 3NF). Example: Student(Course, Instructor) split...",
  "title": "DBMS Normalization",
  "subject": "DBMS",
  "course": "CS301",
  "duration_minutes": 45,
  "external_id": "study-lecture-001",  // used for deduplication
  "object_reference": "objects/lecture3.pdf"  // optional, stored in vault
}
```

Adapter expects at least one of `notes|transcript|content|text|summary`. On success (201): `{"accepted":true,"source_app":"study","memory_id":"...","duplicate":false,...}`. If replayed with same `external_id`, `duplicate:true` with same `memory_id`.

## 5) Query brain (optional)
```json
POST /api/v1/brain/query
Headers: Authorization: Bearer $accessToken
{
  "query": "Explain 3NF with example",
  "context": {},
  "source_app": "study",
  "use_memory": true,
  "persist_interaction": false,
  "memory_limit": 5
}
```
Expect 200 with `answer`, `source` (e.g. `server2_brain`), `user_id`. If 403 → not connected or permission missing; trigger auto-connect + retry.

## 6) Verification checklist
- [ ] Login succeeds, token stored securely
- [ ] `GET /api/v1/apps/study` returns `queries_brain: true` and `default_permissions` includes `brain:read`
- [ ] Auto-connect called once; 201 or 409 handled
- [ ] `GET /api/v1/apps/me` shows `study` active with `brain:read`,`memory:write`,`timeline:write`
- [ ] Ingest returns 201 with `memory_id`
- [ ] Brain query returns 200 (no 403)
- [ ] User isolation verified (another user can't see this data)

## 7) Notes for agent
- Never call `ai_server_url` directly; always go through Server 1.
- Use `external_id` for dedup on repeat syncs.
- Treat 409 on connect as success.
- Don't log tokens/PII in crash reports.
