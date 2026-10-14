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

    async def upsert_for_identity(
        self,
        ai_identity_id: uuid.UUID,
        *,
        object_reference: str,
        storage_type: str = "object",
        checksum: str | None = None,
        mime_type: str | None = None,
        file_size_bytes: int | None = None,
        processing_status: str = "stored",
        encryption_metadata: str | None = None,
    ) -> MemoryVault:
        """Create or update the single vault pointer row for an identity.

        ``memory_vault.ai_identity_id`` is unique, so the vault is modelled as a
        pointer to the identity's current primary object rather than an
        unbounded log; individual memories live in ``brain_memories``.
        """
        existing = await self.get_by_ai_identity_id(ai_identity_id)
        if existing is not None:
            existing.object_reference = object_reference
            existing.storage_type = storage_type
            existing.checksum = checksum
            existing.mime_type = mime_type
            existing.file_size_bytes = file_size_bytes
            existing.processing_status = processing_status
            existing.encryption_metadata = encryption_metadata
            await self.session.flush()
            return existing
        return await self.create(
            ai_identity_id=ai_identity_id,
            object_reference=object_reference,
            storage_type=storage_type,
            checksum=checksum,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            processing_status=processing_status,
            encryption_metadata=encryption_metadata,
        )
