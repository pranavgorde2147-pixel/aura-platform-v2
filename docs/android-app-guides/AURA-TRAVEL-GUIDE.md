# AURA Travel App – Android Studio Integration Guide

**App Slug:** `travel`  
**Category:** Mobile (planned)  
**Queries Brain:** `true`  
**Ingests:** `true`  
**Default Permissions:** `brain:read`, `memory:write`, `files:write`  
**Ingestion Adapter:** `/api/v1/apps/travel/ingest` → `normalize_travel` (FACT, `travel.trip`)

## 1) Auto-connect
```kotlin
POST /api/v1/apps/me
{"module_slug": "travel"}
```

## 2) Ingest trip/itinerary
```json
POST /api/v1/apps/travel/ingest
Authorization: Bearer <access_token>
{
  "destination": "Goa",
  "start_date": "2026-12-20",
  "end_date": "2026-12-27",
  "itinerary": "Beach, food, sightseeing",
  "external_id": "trip-goa-dec-2026",
  "object_reference": "objects/itinerary.pdf"
}
```
Requires `destination|destination_name|place|title`.

## 3) Query brain
```json
POST /api/v1/brain/query
{"query": "What trips do I have planned?", "source_app": "travel", "use_memory": true}
```
