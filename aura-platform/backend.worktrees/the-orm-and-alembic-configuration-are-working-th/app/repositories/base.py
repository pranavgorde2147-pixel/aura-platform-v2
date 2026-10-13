"""Base repository class for database access patterns."""

from __future__ import annotations

import uuid
from typing import Generic, TypeVar, Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Base repository providing common CRUD operations."""

    def __init__(self, session: AsyncSession, model_class: type[T]):
        """Initialize the repository with a session and model class."""
        self.session = session
        self.model_class = model_class

    async def get_by_id(self, id: uuid.UUID) -> T | None:
        """Retrieve a single record by ID."""
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.id == id)
        )
        return result.scalars().first()

    async def create(self, **kwargs: Any) -> T:
        """Create and persist a new record."""
        instance = self.model_class(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id: uuid.UUID, **kwargs: Any) -> T | None:
        """Update an existing record by ID."""
        instance = await self.get_by_id(id)
        if instance is None:
            return None
        for key, value in kwargs.items():
            setattr(instance, key, value)
        await self.session.flush()
        return instance

    async def delete(self, id: uuid.UUID) -> bool:
        """Delete a record by ID."""
        instance = await self.get_by_id(id)
        if instance is None:
            return False
        await self.session.delete(instance)
        await self.session.flush()
        return True

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self.session.commit()

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        await self.session.rollback()
