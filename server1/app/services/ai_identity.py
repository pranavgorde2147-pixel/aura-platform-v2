"""AI Identity service layer for AI identity management."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_identity import AIIdentity
from app.repositories.ai_identity import AIIdentityRepository
from app.core.logging import get_logger

logger = get_logger(__name__)


class AIIdentityService:
    """Service for managing AI identities."""

    def __init__(self, session: AsyncSession):
        """Initialize the service with dependencies."""
        self.session = session
        self.repo = AIIdentityRepository(session)

    async def create_ai_identity(
        self,
        user_id: uuid.UUID,
        display_name: str | None = None,
        is_default: bool = True,
    ) -> AIIdentity:
        """Create a new AI identity for a user while enforcing a single AI identity per user."""
        existing = await self.repo.get_by_user_id(user_id)
        if existing is not None:
            return existing

        identity = await self.repo.create(
            user_id=user_id,
            display_name=display_name or f"Brain-{uuid.uuid4().hex[:8]}",
            is_default=is_default,
        )
        await self.repo.commit()
        logger.info(f"Created AIIdentity {identity.id} for user {user_id}")
        return identity

    async def get_identity_by_user(self, user_id: uuid.UUID) -> AIIdentity | None:
        """Retrieve the AI identity for a user."""
        return await self.repo.get_by_user_id(user_id)

    async def get_identity_by_ai_uuid(self, ai_uuid: uuid.UUID) -> AIIdentity | None:
        """Retrieve an AI identity by ai_uuid."""
        return await self.repo.get_by_ai_uuid(ai_uuid)

    async def get_identity_by_id(self, identity_id: uuid.UUID) -> AIIdentity | None:
        """Retrieve an AI identity by ID."""
        return await self.repo.get_by_id(identity_id)

    async def update_identity(
        self, identity_id: uuid.UUID, **kwargs
    ) -> AIIdentity | None:
        """Update an AI identity."""
        # Protect immutable fields
        kwargs.pop("id", None)
        kwargs.pop("user_id", None)
        kwargs.pop("ai_uuid", None)
        
        identity = await self.repo.update(identity_id, **kwargs)
        if identity:
            await self.repo.commit()
        return identity

    async def set_active(self, identity_id: uuid.UUID) -> AIIdentity | None:
        """Set an AI identity as active."""
        return await self.update_identity(identity_id, status="active")

    async def set_inactive(self, identity_id: uuid.UUID) -> AIIdentity | None:
        """Set an AI identity as inactive."""
        return await self.update_identity(identity_id, status="inactive")
