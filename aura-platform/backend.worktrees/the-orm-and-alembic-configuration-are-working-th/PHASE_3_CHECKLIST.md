# PHASE 3 QUICK REFERENCE CHECKLIST

## Pre-Implementation Checklist

- [ ] Read PHASE_3_AUDIT_REPORT.md (understand the gap)
- [ ] Read PHASE_3_SPECIFICATION.md (understand the design)
- [ ] Read AURA_SERVER_1_STATUS.md (understand the status)
- [ ] Verify current Python environment compiles all files
- [ ] Verify database migrations work

## PHASE 3a: Authentication (Days 1-2)

### Files to Create

- [ ] `app/core/security.py`
  - [ ] Import passlib CryptContext
  - [ ] `hash_password(password: str) -> str` using bcrypt
  - [ ] `verify_password(plain: str, hashed: str) -> bool`
  - [ ] Tests: hash/verify same password, different passwords

- [ ] `app/core/jwt.py`
  - [ ] `create_access_token(user_id: str) -> str` (15 min expiry)
  - [ ] `create_refresh_token(user_id: str) -> str` (7 day expiry)
  - [ ] `decode_token(token: str, token_type: str) -> dict`
  - [ ] Tests: token creation, decoding, expiration

- [ ] `app/schemas/auth.py`
  - [ ] `LoginRequest` (email, password)
  - [ ] `TokenResponse` (access_token, refresh_token, expires_in)
  - [ ] `RefreshRequest` (refresh_token)
  - [ ] `TokenData` (user_id, token_type)

- [ ] `app/services/auth.py`
  - [ ] `AuthService` class
  - [ ] `async authenticate_user(email, password) -> User | None`
  - [ ] `async login(email, password) -> dict` with tokens
  - [ ] `async refresh_token(token: str) -> dict`
  - [ ] `async logout(token: str) -> None`

- [ ] `app/dependencies/auth.py`
  - [ ] `async get_current_user(credentials) -> dict` with user
  - [ ] HTTPBearer security scheme
  - [ ] User lookup and validation
  - [ ] Return user object for endpoints

- [ ] `app/api/v1/auth.py`
  - [ ] `POST /auth/login` → TokenResponse
  - [ ] `POST /auth/refresh` → TokenResponse
  - [ ] `POST /auth/logout` → 204 No Content
  - [ ] Error handling for invalid credentials

### Files to Modify

- [ ] `app/core/config.py`
  - [ ] Add `jwt_secret_key` Field
  - [ ] Add `jwt_algorithm` Field (default HS256)
  - [ ] Add `access_token_expire_minutes` Field (default 15)
  - [ ] Add `refresh_token_expire_days` Field (default 7)

- [ ] `app/api/v1/users.py`
  - [ ] Import `hash_password` from `app.core.security`
  - [ ] Fix user creation to hash password
  - [ ] Add `GET /users/me` protected endpoint
  - [ ] Protect `GET /users/{id}` to enforce ownership
  - [ ] Change user registration to require authentication for some operations

- [ ] `app/api/v1/router.py`
  - [ ] Import auth router
  - [ ] Include auth router in api_router

- [ ] `.env.example`
  - [ ] Add `JWT_SECRET_KEY=...` (min 32 chars)
  - [ ] Add `JWT_ALGORITHM=HS256`
  - [ ] Add `ACCESS_TOKEN_EXPIRE_MINUTES=15`
  - [ ] Add `REFRESH_TOKEN_EXPIRE_DAYS=7`

### Testing

- [ ] Unit tests for password hashing
  - [ ] Same password hashes to different values
  - [ ] Correct password verifies
  - [ ] Wrong password doesn't verify

- [ ] Unit tests for JWT
  - [ ] Access token created successfully
  - [ ] Refresh token created successfully
  - [ ] Token decoding works
  - [ ] Expired token raises error
  - [ ] Invalid token raises error

- [ ] Integration tests for auth endpoints
  - [ ] POST /auth/login with correct credentials → tokens
  - [ ] POST /auth/login with wrong credentials → 401
  - [ ] POST /auth/refresh with valid token → new access token
  - [ ] POST /auth/logout succeeds → future login required
  - [ ] GET /users/me without token → 401
  - [ ] GET /users/me with valid token → user data
  - [ ] GET /users/{other_user_id} → 403 Forbidden

### Verification

- [ ] `python -m py_compile app/**/*.py` → Success
- [ ] All imports resolve correctly
- [ ] `alembic current` → Works
- [ ] `alembic history` → Works
- [ ] `alembic upgrade head` → Works
- [ ] `alembic downgrade base` → Works
- [ ] `pytest tests/test_auth.py` → All pass
- [ ] No plain-text passwords in database

---

## PHASE 3b: Brain API (Days 3-4)

### Files to Create

- [ ] `app/schemas/brain_request.py`
  - [ ] `BrainQueryRequest` (query, context, metadata)
  - [ ] `BrainQueryResponse` (query_id, status, result, requires_ai_server)
  - [ ] `BrainIngestRequest` (source_app, content_type, data, metadata)
  - [ ] `BrainIngestResponse` (ingest_id, status, classified_as)

- [ ] `app/core/brain_provider.py`
  - [ ] `BrainProvider` abstract base class
  - [ ] `async process_query(query, context) -> str`
  - [ ] `async ingest_data(content_type, data) -> dict`
  - [ ] `MockBrainProvider` for development
  - [ ] Tests for mock provider

- [ ] `app/services/brain_query.py`
  - [ ] `BrainQueryService` class
  - [ ] `async query(ai_identity_id, query, context) -> dict`
  - [ ] Verify Brain exists and is active
  - [ ] Gather relevant personal context
  - [ ] Pass through provider

- [ ] `app/services/brain_ingest.py`
  - [ ] `BrainIngestService` class
  - [ ] `async ingest(ai_identity_id, source_app, content_type, data, metadata) -> dict`
  - [ ] Verify Brain exists
  - [ ] Let Brain classify data
  - [ ] Store according to classification

- [ ] `app/api/v1/brain_api.py`
  - [ ] `POST /brain/query` → BrainQueryResponse
  - [ ] `POST /brain/ingest` → BrainIngestResponse
  - [ ] Require authentication on both endpoints
  - [ ] Derive ai_identity from current_user
  - [ ] Error handling

### Testing

- [ ] Unit tests for brain provider
  - [ ] MockBrainProvider returns mock responses
  - [ ] Query processing works
  - [ ] Data ingestion works

- [ ] Integration tests for brain API
  - [ ] POST /brain/query without auth → 401
  - [ ] POST /brain/query with auth → response
  - [ ] POST /brain/ingest without auth → 401
  - [ ] POST /brain/ingest with auth → response
  - [ ] Query includes relevant context
  - [ ] Ingest classifies data correctly

### Verification

- [ ] `pytest tests/test_brain_api.py` → All pass
- [ ] Brain query works end-to-end
- [ ] Brain ingest works end-to-end
- [ ] Provider abstraction in place

---

## PHASE 3c: Security Enforcement (Concurrent)

- [ ] All services validate user ownership
- [ ] All endpoints enforce authentication
- [ ] Audit logging for sensitive operations
- [ ] User A cannot access User B's data
- [ ] Brain A cannot access Brain B's data

---

## Final Verification

- [ ] All Python syntax valid
- [ ] All imports resolve
- [ ] All tests passing (>80% coverage)
- [ ] Alembic commands work
- [ ] No regressions from PHASE 2
- [ ] Documentation updated
- [ ] Commit message includes "PHASE 3" deliverables

---

## Commit Message Template

```
feat: implement PHASE 3 - authentication and brain API

PHASE 3a: Authentication & Password Hashing
- Bcrypt password hashing on user registration
- JWT token generation (access 15min, refresh 7day)
- Login/logout/refresh endpoints
- Route protection middleware
- Current user dependency for protected endpoints
- All personal endpoints require authentication

PHASE 3b: Personal Brain API
- Brain Query endpoint (POST /api/v1/brain/query)
- Brain Ingest endpoint (POST /api/v1/brain/ingest)
- Brain provider abstraction for future LLM integration
- Mock provider for development
- Brain controls all personal data access

PHASE 3c: Security Enforcement
- User ownership validation in all services
- Brain isolation checks
- Audit logging for sensitive operations
- User A cannot access User B's data
- Brain A cannot access Brain B's data

Key Architectural Achievement:
- Apps now communicate ONLY through Personal Brain
- Brain is now an intelligence boundary (not just data router)
- Provider pattern enables future LLM swapping

Tests:
- Unit tests for auth (hash, JWT, tokens)
- Integration tests for auth endpoints
- Integration tests for brain API
- Security isolation tests

Database:
- No schema changes required
- Alembic migrations clean
- Password_hash field now properly used

Documentation:
- API documentation updated
- Architecture guide updated
- Auth flow diagram added
- Brain API examples added

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

