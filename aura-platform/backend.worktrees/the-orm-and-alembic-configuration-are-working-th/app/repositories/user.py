"""User repository for database access to user records."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """Repository for User model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize UserRepository with async session."""
        super().__init__(session, User)

    async def get_by_email(self, email: str) -> User | None:
        """Retrieve a user by email address."""
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalars().first()

    async def get_active_users(self, limit: int = 100) -> list[User]:
        """Retrieve all active users."""
        result = await self.session.execute(
            select(User).where(User.is_active == True).limit(limit)
        )
        return result.scalars().all()

    async def email_exists(self, email: str) -> bool:
        """Check if a user with the given email exists."""
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalars().first() is not None
