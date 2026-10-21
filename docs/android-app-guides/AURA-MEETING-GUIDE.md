# AURA Meeting App – Android Studio Integration Guide

**App Slug:** `meeting`  
**Category:** Mobile (live)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `files:write`  
**Ingestion Adapter:** `/api/v1/apps/meeting/ingest` → `normalize_meeting` (FACT, `meeting.recorded`). Builds content from transcript + Decisions + Action items.

## 1) Auto-connect
After login:
```kotlin
POST /api/v1/apps/me
Authorization: Bearer <access_token>
{"module_slug": "meeting"}
```
Handle 201/409.

## 2) Ingest meeting data
```json
POST /api/v1/apps/meeting/ingest
Authorization: Bearer <access_token>
{
  "title": "Sprint Planning",
  "transcript": "Discussed auth, rate limits, tests...",
  "decisions": ["Use JWT rotation", "Deploy to Render on merge"],
  "action_items": ["Add Redis cache", "Update API docs"],
  "participants": ["Alice", "Bob"],
  "occurred_at": "2026-10-21T09:00:00Z",
  "external_id": "meet-2026-10-21-sprint-01",
  "object_reference": "objects/transcript.txt"
}
```
Requires `transcript|summary|notes|content|text`. Creates FACT memory + timeline/knowledge entries. Dedup by `external_id`.

## 3) Query brain
```json
POST /api/v1/brain/query
Authorization: Bearer <access_token>
{
  "query": "What were our action items from last meeting?",
  "source_app": "meeting",
  "use_memory": true,
  "memory_limit": 10
}
```

## 4) Verification
- [ ] Connected with `brain:read`
- [ ] Ingest with decisions/action_items returns 201, `memory_id` present
- [ ] Query returns 200 with relevant context
