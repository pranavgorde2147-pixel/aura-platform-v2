# PHASE 1: REPOSITORY AUDIT

## Current Architecture (Observed)

The repository currently has three services as documented:

### SERVER 1 (server1/) - AURA CORE/API
- Entry: app/main.py:create_app()
- Port: 8000
- Purpose: Auth, users, Personal Brain, files API, health
- Key features:
  - FastAPI with JWT auth (access/refresh tokens)
  - User management, AI identity, Personal AI Brain models
  - PersonalBrainService orchestrates queries via providers
  - BrainMemory model for structured persistent memories (fact/interpretation/event)
  - MemoryVault model for object references
  - Files API proxies to Server 3 with owner isolation
  - Brain client calls Server 2 for inference
  - Health checks aggregate DB, Redis, Qdrant, AI server, Storage server
  - Supports external AI fallback when Server 2 unavailable

### SERVER 2 (server2/) - INFERENCE
- Entry: app/main.py
- Port: 8001
- Purpose: LLM inference (Qwen3.5-4B) with internal token auth
- Endpoint: POST /v1/brain/infer
- Strips sensitive context keys (vault, password, secret, token, jwt)
- Supports external LLM fallback when in skip mode
- Health/ready endpoints

### SERVER 3 (server3/) - OBJECT STORAGE
- Entry: app/main.py:create_app()
- Port: 8002
- Purpose: Object storage (local filesystem, S3 supported)
- Internal token + X-Aura-Owner-Id for per-user isolation
- Metadata stored in SQLite/Postgres (StoredObject)
- Endpoints: /v1/objects (upload/list), /v1/objects/{id} (download/delete), /v1/objects/{id}/metadata

## Existing Working Functionality

- Authentication system (JWT, login/refresh/logout)
- User CRUD with ownership checks
- AI Identity per user (1:1)
- Personal AI Brain tied to AI Identity
- BrainMemory persistence with kind/source (FACT/INTERPRETATION/EVENT, USER/BRAIN/APP)
- Memory retrieval (search by keywords, recent)
- Explicit opt-in for memory use/persistence (use_memory, persist_interaction)
- Files API with owner isolation via Server 3
- Real Server 2 integration with fallback
- Cross-user isolation enforced at multiple layers
- Database migrations with Alembic

## Dependencies Between Servers

- Server 1 calls Server 2 via BrainClient (httpx) using INTERNAL_API_TOKEN, passes user_id, query, context
- Server 1 calls Server 3 via StorageClient using STORAGE_API_TOKEN/effective token, passes X-Aura-Owner-Id header
- Server 2 validates Bearer token, enforces model name, validates UUIDs
- Server 3 validates Bearer token + owner_id, enforces per-owner filtering

## Storage Implementation Details

Server 3 implements full object storage with:
- Local filesystem backend with path safety
- Metadata table (stored_objects) with owner_id, checksum, etc.
- Per-user isolation enforced in queries
- Size limits, content-type allowlist
- Cross-user isolation tested

Server 1's MemoryVault is a different concept (metadata record pointing to object reference, currently separate from BrainMemory). BrainMemory is the new structured memory approach.

## Meeting/Summarization Workflows

Minimal explicit meeting/summarization API endpoints found. "meeting_ai" is listed as a future Qdrant collection name, but no specific meeting/summarization endpoints or services in the API surface beyond what's described. The core AURA functionality centers around brain/memory/files/auth.

## Database Models

Server 1 has: User, AIIdentity, PersonalAIBrain, BrainMemory, MemoryVault, KnowledgeGraph, Timeline, Device, ModulePermission, Session, InstalledModule.

BrainMemory (newer) stores actual memory content with kinds. MemoryVault stores object references.

## Tests

Server 1 tests: auth/brain boundary, brain memory, fallback, config, files API, model architecture, cardinality. Integration tests exist for backend DB and storage end-to-end (real Server 3 process + embedded Postgres in some contexts?).

Server 2 tests: health/inference schema, validation, isolation.
Server 3 tests: objects lifecycle, ownership, persistence, limits.

## Migration Risks

- Server 3 currently handles file storage via HTTP API. If we eliminate Server 3 as a separate deployed app, we need to migrate its storage capabilities into Server 2 (per target). But also need to consider - Server 3 does local filesystem + metadata DB. Moving into Server 2 means Server 2 takes on storage responsibility.
- MemoryVault vs BrainMemory - they serve different purposes; need to understand if both needed.
- Files API in Server 1 currently proxies to Server 3 - this contract must be preserved if possible.
- Existing working endpoints must not break.

## Key Observations

1. Current architecture is 3 servers. Target is 2 servers (Server 1 + Server 2). Server 3's capabilities need to be migrated into Server 2.
2. Strong existing patterns for per-user isolation (owner_id, ai_identity_id filtering).
3. BrainMemory already implements the structured memory model requested (fact/interpretation/event, controlled operations).
4. Real inference integration exists (Server 2) - must preserve.
5. Authentication is solid.
6. Explicit opt-in for memory operations is already implemented (good design).
