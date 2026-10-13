"""Memory Vault repository for database access to memory vault records."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.memory_vault import MemoryVault
from app.repositories.base import BaseRepository


class MemoryVaultRepository(BaseRepository[MemoryVault]):
    """Repository for MemoryVault model operations."""

    def __init__(self, session: AsyncSession):
        """Initialize MemoryVaultRepository with async session."""
        super().__init__(session, MemoryVault)

    async def get_by_ai_identity_id(self, ai_identity_id: uuid.UUID) -> MemoryVault | None:
        """Retrieve the memory vault for a specific AI identity."""
        result = await self.session.execute(
            select(MemoryVault).where(
                MemoryVault.ai_identity_id == ai_identity_id
            )
        )
        return result.scalars().first()

    async def get_by_object_reference(self, object_reference: str) -> MemoryVault | None:
        """Retrieve a memory vault entry by object reference."""
        result = await self.session.execute(
            select(MemoryVault).where(
                MemoryVault.object_reference == object_reference
            )
        )
        return result.scalars().first()

    async def get_pending_processing(self, limit: int = 50) -> list[MemoryVault]:
        """Retrieve memory entries pending processing."""
        result = await self.session.execute(
            select(MemoryVault)
            .where(MemoryVault.processing_status == "pending")
            .limit(limit)
        )
        return result.scalars().all()
