"""User service layer for user management."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.ai_identity import AIIdentity
from app.repositories.user import UserRepository
from app.repositories.ai_identity import AIIdentityRepository
from app.repositories.personal_ai_brain import PersonalAIBrainRepository
from app.core.logging import get_logger

logger = get_logger(__name__)


class UserService:
    """Service for managing users and their identities."""

    def __init__(self, session: AsyncSession):
        """Initialize the service with dependencies."""
        self.session = session
        self.user_repo = UserRepository(session)
        self.identity_repo = AIIdentityRepository(session)
        self.brain_repo = PersonalAIBrainRepository(session)

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str | None = None,
    ) -> User:
        """Create a new user with one AURA identity and one Personal Brain binding."""
        user = await self.user_repo.create(
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            is_active=True,
        )
        await self.user_repo.commit()

        identity = await self.identity_repo.create(
            user_id=user.id,
            display_name=full_name or f"AURA-{user.email.split('@', 1)[0]}",
            is_default=True,
        )
        await self.identity_repo.commit()

        existing_brain = await self.brain_repo.get_by_ai_identity_id(identity.id)
        if existing_brain is None:
            brain_name = full_name or "Personal Brain"
            brain = await self.brain_repo.create(
                ai_identity_id=identity.id,
                brain_name=brain_name,
                version="1.0",
                state="initializing",
            )
            await self.brain_repo.commit()
            logger.info(f"Created Personal Brain {brain.id} for user {user.id}")

        logger.info(f"Created user {user.id} with email {email} and bound AI identity {identity.id}")
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        """Retrieve a user by email."""
        return await self.user_repo.get_by_email(email)

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        """Retrieve a user by ID."""
        return await self.user_repo.get_by_id(user_id)

    async def get_user_with_identity(self, user_id: uuid.UUID) -> tuple[User | None, AIIdentity | None]:
        """Retrieve a user with their AI identity."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return None, None
        
        identity = await self.identity_repo.get_by_user_id(user_id)
        return user, identity

    async def user_email_exists(self, email: str) -> bool:
        """Check if a user with the given email exists."""
        return await self.user_repo.email_exists(email)

    async def update_user(self, user_id: uuid.UUID, **kwargs) -> User | None:
        """Update user information."""
        # Remove sensitive fields from being updated directly
        kwargs.pop("id", None)
        kwargs.pop("password_hash", None)
        
        user = await self.user_repo.update(user_id, **kwargs)
        if user:
            await self.user_repo.commit()
        return user

    async def deactivate_user(self, user_id: uuid.UUID) -> User | None:
        """Deactivate a user account."""
        user = await self.user_repo.update(user_id, is_active=False)
        if user:
            await self.user_repo.commit()
        logger.info(f"Deactivated user {user_id}")
        return user
