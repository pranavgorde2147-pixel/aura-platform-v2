# PHASE 2: IMPLEMENTATION PLAN

## Goal
Evolve to 2 servers: Server 1 (AURA CORE) + Server 2 (AURA BRAIN with inference + memory + storage/vault). Migrate Server 3 capabilities into Server 2 while preserving existing AURA Core functionality and contracts.

## Key Principles
1. Do NOT do wholesale rewrite - incremental changes only
2. Preserve existing working endpoints, schemas, auth
3. Preserve real inference (Server 2)
4. Migrate Server 3 storage capabilities into Server 2
5. Keep per-user isolation and security boundaries
6. Maintain backward compatibility where practical

## Migration Strategy for Server 3 -> Server 2

Server 1 currently calls Server 3 for files via StorageClient (expects /v1/objects endpoints, X-Aura-Owner-Id header, bearer token). To go from 3 to 2 servers, we need Server 2 to expose the same storage API.

Recommended: Integrate Server 3's storage code into Server 2
- Copy/adapt storage modules (storage backends, database models for stored_objects, services, API routes) into Server 2
- Add storage router to Server 2 app (mounted at appropriate paths)
- Server 1's StorageClient will point to Server 2's storage endpoint (change STORAGE_SERVER_URL to Server 2)
- Keep the same API contract so Files API in Server 1 remains unchanged

## Detailed Steps

### Step 1: Extend Server 2 with storage functionality (migrated from Server 3)
Copy to server2/app/:
- storage/ (base.py, local.py, s3.py, etc. from server3/app/storage)
- database.py for stored objects (or integrate into existing)
- models for stored objects
- services/objects.py
- api/routes for objects with same security model (internal token + owner validation)
- security/dependencies for storage

Also need to handle DB setup - Server 2 might need its own storage DB. Could use SQLite by default like Server 3.

### Step 2: Enhance Server 2 main app to include storage routes
Add storage router. Also add health/ready that consider storage if needed.

### Step 3: Enhance Server 1 Brain/Memories APIs
Add missing operations:
- Update memory (PUT/PATCH /api/v1/brain/memory/{id})
- Delete memory (DELETE /api/v1/brain/memory/{id})
- Get single memory
- Search memories

Also add models for app connections/permissions for cross-app support per requirements.

### Step 4: Update configs and deployment
- Update env examples (.env.example files)
- Update docker-compose files (remove Server 3 dependency from Server 1 compose, or keep for now? But target is 2 servers)
- Update README/DEPLOYMENT docs
- Update render.yaml if needed

### Step 5: Add tests for new functionality
Add tests for: update/delete memories, cross-app sharing, isolation, external API routing without vault data
