# AURA Daily Diary App – Android Studio Integration Guide

**App Slug:** `diary`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `timeline:write`  
**Ingestion Adapter:** `/api/v1/apps/diary/ingest` → `normalize_diary` (INTERPRETATION, `diary.entry`)

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "diary"}
```

## 2) Ingest diary entry
```json
POST /api/v1/apps/diary/ingest
Authorization: Bearer <access_token>
{
  "entry": "Today was peaceful. I spent time reading...",
  "mood": "calm",
  "date": "2026-10-21",
  "external_id": "diary-2026-10-21"
}
```
Requires `entry|text|content|body`. Stored as `INTERPRETATION` (personal reflection).

## 3) Query brain
```json
POST /api/v1/brain/query
{"query": "What was my mood last week?", "source_app": "diary", "use_memory": true}
```
