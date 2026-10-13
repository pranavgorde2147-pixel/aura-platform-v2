# PHASE 3: AUTHENTICATION & SECURITY — DETAILED SPECIFICATION

**Duration**: 5-7 days  
**Priority**: CRITICAL - Blocks all future development  
**Scope**: Implement authentication, authorization, and Brain API  

---

## PHASE 3a: PASSWORD HASHING & JWT AUTHENTICATION

### 1. Security Module (`app/core/security.py`)

Create comprehensive password and security utilities:

```python
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)
```

**Tests Required**:
- Hash and verify same password → True
- Verify different password → False
- Hash same password twice → Different hashes (salt randomness)

---

### 2. JWT Module (`app/core/jwt.py`)

Token generation and validation:

```python
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from app.core.config import get_settings

def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create a short-lived JWT access token."""
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": user_id,
        "type": "access",
        "exp": expire
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def create_refresh_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create a long-lived JWT refresh token."""
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(days=settings.refresh_token_expire_days)
    
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def decode_token(token: str, token_type: str = "access") -> dict:
    """Decode and validate a JWT token."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != token_type:
            raise ValueError("Invalid token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")
```

**Configuration Required** (in `app/core/config.py`):
```python
jwt_secret_key: str = Field(..., alias="JWT_SECRET_KEY")
jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
access_token_expire_minutes: int = Field(default=15, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")
```

---

### 3. Auth Schema (`app/schemas/auth.py`)

Request/response models:

```python
from pydantic import BaseModel, Field
import uuid
from datetime import datetime

class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=255)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds

class RefreshRequest(BaseModel):
    refresh_token: str

class TokenData(BaseModel):
    user_id: str
    token_type: str
```

---

### 4. Auth Service (`app/services/auth.py`)

Authentication business logic:

```python
from app.repositories.user import UserRepository
from app.models.user import User
from app.core.security import hash_password, verify_password
from app.core.jwt import create_access_token, create_refresh_token
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
    
    async def authenticate_user(self, email: str, password: str) -> User | None:
        """Authenticate user by email and password."""
        user = await self.user_repo.get_by_email(email)
        if not user:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user
    
    async def login(self, email: str, password: str) -> dict:
        """Authenticate and return tokens."""
        user = await self.authenticate_user(email, password)
        if not user:
            raise ValueError("Invalid email or password")
        
        access_token = create_access_token(str(user.id))
        refresh_token = create_refresh_token(str(user.id))
        
        # TODO: Store refresh token in Redis for revocation
        # TODO: Update user.last_login_at
        
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 15 * 60  # 15 minutes
        }
    
    async def refresh_token(self, refresh_token: str) -> dict:
        """Generate new access token from refresh token."""
        payload = decode_token(refresh_token, token_type="refresh")
        user_id = payload["sub"]
        
        # TODO: Verify refresh token not revoked in Redis
        
        access_token = create_access_token(user_id)
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 15 * 60
        }
    
    async def logout(self, refresh_token: str) -> None:
        """Invalidate a refresh token."""
        # TODO: Add refresh_token to Redis blacklist
        pass
```

---

### 5. Auth Dependency (`app/dependencies/auth.py`)

Extract current user from JWT:

```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthCredentials
from app.core.jwt import decode_token
from app.repositories.user import UserRepository
from app.infrastructure.database.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthCredentials = Depends(security),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Extract and validate current user from JWT."""
    try:
        payload = decode_token(credentials.credentials, token_type="access")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    
    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    return {"user_id": user_id, "user": user}
```

---

### 6. Auth Endpoints (`app/api/v1/auth.py`)

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database.session import get_db
from app.services.auth import AuthService
from app.schemas.auth import LoginRequest, TokenResponse, RefreshRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    session: AsyncSession = Depends(get_db),
):
    """Authenticate user and return JWT tokens."""
    service = AuthService(session)
    try:
        tokens = await service.login(request.email, request.password)
        return tokens
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshRequest,
    session: AsyncSession = Depends(get_db),
):
    """Get new access token using refresh token."""
    service = AuthService(session)
    try:
        tokens = await service.refresh_token(request.refresh_token)
        return tokens
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: RefreshRequest,
    session: AsyncSession = Depends(get_db),
):
    """Invalidate a refresh token."""
    service = AuthService(session)
    await service.logout(request.refresh_token)
```

---

## PHASE 3b: PROTECTED ROUTES & USER ISOLATION

### 7. Update User Endpoints

Modify `/api/v1/users.py` to:
1. Require authentication for GET endpoints (except public registration)
2. Hash passwords on user creation
3. Enforce user ownership

```python
@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: UserCreate,
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Create a new user account."""
    service = UserService(session)
    
    # Check if email already exists
    if await service.user_email_exists(data.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # FIXED: Hash password
    hashed_password = hash_password(data.password)
    
    user = await service.create_user(
        email=data.email,
        password_hash=hashed_password,  # Now hashed!
        full_name=data.full_name,
    )
    return user

@router.get("/users/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: dict = Depends(get_current_user),
) -> UserResponse:
    """Get authenticated user's profile."""
    return current_user["user"]

@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Get a user by ID (only allow self or admin)."""
    # SECURITY: Enforce ownership
    if user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    service = UserService(session)
    user = await service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
```

---

## PHASE 3c: BRAIN INTELLIGENCE BOUNDARY

### 8. Brain Query/Ingest Schema (`app/schemas/brain_request.py`)

```python
from pydantic import BaseModel, Field
from typing import Optional, Any
import uuid
from datetime import datetime

class BrainQueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    context: Optional[dict] = None
    metadata: Optional[dict] = None

class BrainQueryResponse(BaseModel):
    query_id: str
    status: str  # "processing", "completed", "error"
    result: Optional[Any] = None
    requires_ai_server: bool = False  # For future Server 2
    created_at: datetime

class BrainIngestRequest(BaseModel):
    source_app: str = Field(..., min_length=1)
    content_type: str = Field(...)  # "lecture", "meeting", "health_event", etc.
    data: dict = Field(...)
    metadata: Optional[dict] = None

class BrainIngestResponse(BaseModel):
    ingest_id: str
    status: str
    created_at: datetime
```

---

### 9. Brain Provider (`app/core/brain_provider.py`)

Abstraction for future LLM integration:

```python
from abc import ABC, abstractmethod
from typing import Optional, Any

class BrainProvider(ABC):
    """Abstract interface for Brain inference providers."""
    
    @abstractmethod
    async def process_query(self, query: str, context: dict) -> str:
        """Process a user query through the Brain."""
        pass
    
    @abstractmethod
    async def ingest_data(self, content_type: str, data: dict) -> dict:
        """Process incoming data from an application."""
        pass

class MockBrainProvider(BrainProvider):
    """Development/testing provider that returns deterministic responses."""
    
    async def process_query(self, query: str, context: dict) -> str:
        """Return mock response for testing."""
        return f"Mock response to: {query}"
    
    async def ingest_data(self, content_type: str, data: dict) -> dict:
        """Mock ingestion response."""
        return {
            "status": "processed",
            "content_type": content_type,
            "classified_as": "knowledge",  # or "memory", "timeline"
        }

# Later: LocalTinyLLMProvider, RemoteAIProvider, etc.
```

---

### 10. Brain Query Service (`app/services/brain_query.py`)

```python
from app.core.brain_provider import BrainProvider
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.repositories.memory_vault import MemoryVaultRepository
from app.repositories.knowledge_graph import KnowledgeGraphRepository
from app.repositories.timeline import TimelineRepository
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

class BrainQueryService:
    def __init__(self, session: AsyncSession, provider: BrainProvider):
        self.session = session
        self.provider = provider
        self.brain_repo = PersonalAIBrainRepository(session)
        self.memory_repo = MemoryVaultRepository(session)
        self.knowledge_repo = KnowledgeGraphRepository(session)
        self.timeline_repo = TimelineRepository(session)
    
    async def query(self, ai_identity_id: uuid.UUID, query: str, context: dict = None) -> dict:
        """
        Process a user query through their Personal Brain.
        
        The Brain is the ONLY interface for accessing personal data.
        """
        # Verify Brain exists and is active
        brain = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        if not brain or brain.status.value != "active":
            raise ValueError("Brain not available")
        
        # Gather relevant personal context
        memory = await self.memory_repo.get_by_ai_identity_id(ai_identity_id)
        knowledge = await self.knowledge_repo.get_by_ai_identity_id(ai_identity_id)
        timeline = await self.timeline_repo.get_recent_entries(ai_identity_id, hours=24)
        
        # Build context for the Brain
        brain_context = {
            "query": query,
            "user_id": brain.ai_identity.user_id,  # For ownership verification
            "memory_available": memory is not None,
            "knowledge_available": knowledge is not None,
            "recent_events": len(timeline),
            **(context or {})
        }
        
        # Pass through Brain provider
        response = await self.provider.process_query(query, brain_context)
        
        return {
            "query_id": str(brain.id),
            "status": "completed",
            "result": response,
            "requires_ai_server": False,  # TODO: Determine if Server 2 needed
        }
```

---

### 11. Brain Ingest Service (`app/services/brain_ingest.py`)

```python
from app.core.brain_provider import BrainProvider
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

class BrainIngestService:
    def __init__(self, session: AsyncSession, provider: BrainProvider):
        self.session = session
        self.provider = provider
        self.brain_repo = PersonalAIBrainRepository(session)
    
    async def ingest(
        self,
        ai_identity_id: uuid.UUID,
        source_app: str,
        content_type: str,
        data: dict,
        metadata: dict = None
    ) -> dict:
        """
        Ingest application data into the Personal Brain.
        
        The Brain decides how to organize/classify the data.
        Applications do NOT decide the final storage location.
        """
        # Verify Brain exists
        brain = await self.brain_repo.get_by_ai_identity_id(ai_identity_id)
        if not brain:
            raise ValueError("Brain not found")
        
        # Let Brain classify and organize
        classification = await self.provider.ingest_data(content_type, data)
        
        # TODO: Store data according to Brain's classification
        # - If "memory": store in MemoryVault
        # - If "knowledge": store in KnowledgeGraph
        # - If "timeline": store in Timeline
        # - If "context": update BrainContext
        
        return {
            "ingest_id": str(brain.id),
            "status": classification["status"],
            "classified_as": classification["classified_as"],
        }
```

---

### 12. Brain API Endpoints (`app/api/v1/brain_api.py`)

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database.session import get_db
from app.dependencies.auth import get_current_user
from app.services.brain_query import BrainQueryService
from app.services.brain_ingest import BrainIngestService
from app.services.ai_identity import AIIdentityService
from app.core.brain_provider import MockBrainProvider
from app.schemas.brain_request import (
    BrainQueryRequest,
    BrainQueryResponse,
    BrainIngestRequest,
    BrainIngestResponse,
)

router = APIRouter(prefix="/brain", tags=["Brain"])
provider = MockBrainProvider()  # TODO: Load from config

@router.post("/query", response_model=BrainQueryResponse)
async def query_brain(
    request: BrainQueryRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Query the Personal Brain.
    
    Apps MUST use this endpoint to access personal context.
    They do NOT query Memory/Knowledge/Timeline directly.
    """
    service = BrainQueryService(session, provider)
    identity_service = AIIdentityService(session)
    
    # Get user's AI Identity
    identity = await identity_service.get_identity_by_user_id(current_user["user_id"])
    if not identity:
        raise HTTPException(status_code=404, detail="AI Identity not found")
    
    try:
        response = await service.query(
            identity.id,
            request.query,
            request.context
        )
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/ingest", response_model=BrainIngestResponse)
async def ingest_to_brain(
    request: BrainIngestRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Ingest application data into the Personal Brain.
    
    The Brain decides how to organize/classify the data.
    """
    service = BrainIngestService(session, provider)
    identity_service = AIIdentityService(session)
    
    # Get user's AI Identity
    identity = await identity_service.get_identity_by_user_id(current_user["user_id"])
    if not identity:
        raise HTTPException(status_code=404, detail="AI Identity not found")
    
    try:
        response = await service.ingest(
            identity.id,
            request.source_app,
            request.content_type,
            request.data,
            request.metadata
        )
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

---

## PHASE 3d: CONFIGURATION & ROUTER UPDATES

### 13. Configuration Updates (`app/core/config.py`)

Add JWT settings:

```python
jwt_secret_key: str = Field(..., alias="JWT_SECRET_KEY")
jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
access_token_expire_minutes: int = Field(default=15, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")
```

Update `.env.example`:
```
JWT_SECRET_KEY=your-secret-key-here-min-32-chars
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
```

---

### 14. Router Updates (`app/api/v1/router.py`)

```python
from fastapi import APIRouter
from app.api.v1.health import router as health_router
from app.api.v1.users import router as users_router
from app.api.v1.identity import router as identity_router
from app.api.v1.auth import router as auth_router
from app.api.v1.brain_api import router as brain_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(users_router)
api_router.include_router(identity_router)
api_router.include_router(auth_router)
api_router.include_router(brain_router)
```

---

## TESTING PLAN

### Unit Tests (`tests/test_auth.py`)

```python
pytest.mark.asyncio
async def test_hash_password():
    pwd = "my_password"
    hashed = hash_password(pwd)
    assert verify_password(pwd, hashed)
    assert not verify_password("wrong", hashed)

async def test_create_access_token():
    token = create_access_token("user-123")
    payload = decode_token(token, "access")
    assert payload["sub"] == "user-123"

async def test_login_success(session):
    # Create user with hashed password
    # Login with correct password → tokens
    pass

async def test_login_failure(session):
    # Login with wrong password → error
    pass
```

### Integration Tests (`tests/test_auth_endpoints.py`)

```python
async def test_login_endpoint():
    # POST /api/v1/auth/login → tokens

async def test_protected_endpoint_without_token():
    # GET /api/v1/users/me without token → 401

async def test_protected_endpoint_with_token():
    # GET /api/v1/users/me with valid token → user data

async def test_brain_query_endpoint():
    # POST /api/v1/brain/query → brain response

async def test_brain_ingest_endpoint():
    # POST /api/v1/brain/ingest → ingestion confirmation
```

---

## SECURITY CHECKLIST

- [ ] Passwords hashed with bcrypt (never plain text)
- [ ] JWT tokens signed with strong secret
- [ ] Access tokens expire quickly (15 min)
- [ ] Refresh tokens longer-lived (7 days)
- [ ] Logout revokes refresh tokens
- [ ] All personal endpoints require authentication
- [ ] User cannot access other users' data
- [ ] Brain query/ingest only available to authenticated users
- [ ] Audit logging on auth operations
- [ ] No sensitive data in logs
- [ ] Rate limiting on auth endpoints (future)

---

## DEFINITION OF DONE

Phase 3 is complete when:

- [ ] Bcrypt password hashing working
- [ ] JWT tokens working (access + refresh)
- [ ] Login/logout/refresh endpoints functional
- [ ] User profile endpoint protected (/users/me)
- [ ] All user endpoints require authentication
- [ ] Brain query API working
- [ ] Brain ingest API working
- [ ] Brain provider abstraction created
- [ ] Mock provider for development
- [ ] All tests passing
- [ ] No plain-text passwords in DB
- [ ] Audit logging implemented
- [ ] Documentation complete

