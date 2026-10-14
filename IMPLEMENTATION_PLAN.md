# IMPLEMENTATION PLAN (PHASE 2+3)

## Completed (Server 1)
- Added MemoryUpdateRequest schema
- Added update_memory_for_user, delete_memory_for_user, get_memory_for_user, search_memories_for_user to PersonalBrainService
- Added memory CRUD/search endpoints in brain API
- All existing brain memory tests still pass

## Next Steps

### 1. Migrate Server 3 storage into Server 2
Need to add storage capabilities to Server 2 so it can serve /v1/objects endpoints with same contract (internal token + X-Aura-Owner-Id). This allows Server 1 to point STORAGE_SERVER_URL to Server 2.

Copy from server3:
- app/storage/ (base, local, s3)
- app/config.py (storage config)
- app/database.py with StoredObject model and DB setup
- app/services/objects.py
- app/api/objects.py, app/api/health.py, app/api/router.py
- app/security.py
- app/schemas.py

Adapt to Server 2's structure (Server 2 already has app/core, app/schemas etc). Also need to integrate routes into Server 2 main.py or add router.

### 2. Add storage models to Server 2 DB if needed? 
Server 2 might need its own database for storage metadata. Server 3 uses SQLite by default (STORAGE_DATABASE_URL) or Postgres. For flexibility, keep similar setup.

### 3. Update Server 2 main.py to include storage routes
Add objects router. Also update health/ready if needed. Maintain existing /v1/brain/infer, /health, /ready.

### 4. Update Server 1 config/defaults
In Server 1 docker-compose and env, STORAGE_SERVER_URL currently points to aura-server3:8002. Need to update to point to Server 2 (aura-server2:8001 or new path). But storage endpoints will be on Server 2 - so either mount under /v1/objects on Server 2 or have separate base URL. Better to add storage routes to Server 2 - maybe under /v1/storage/objects or also expose at /v1/objects? Server 1 calls to {base_url}/v1/objects. So if we point STORAGE_SERVER_URL to http://aura-server2:8001, then need /v1/objects on Server 2.

So add storage API routes to Server 2 (or mount as /v1/objects).

### 5. Add application connections for cross-app support
Model for app permissions per user - allows multiple apps to use same brain with scoped access.

### 6. Tests and docs
Update tests, env examples, docker-compose files, README/DEPLOYMENT.
