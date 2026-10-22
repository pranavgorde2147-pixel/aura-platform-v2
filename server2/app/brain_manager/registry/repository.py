"""Database access for the Brain registry.

The registry table is the authoritative record of who owns which Brain. Every
read and write in this module filters on ``brain_id`` *and* ``owner_id`` so a
stale or forged identifier can never reach another Brain's data.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage_db import ManagedBrain


async def get_by_brain_id(
    session: AsyncSession, brain_id: uuid.UUID
) -> ManagedBrain | None:
    result = await session.execute(
        select(ManagedBrain).where(ManagedBrain.brain_id == brain_id)
    )
    return result.scalars().first()


async def get_by_owner(session: AsyncSession, owner_id: uuid.UUID) -> ManagedBrain | None:
    result = await session.execute(
        select(ManagedBrain).where(ManagedBrain.owner_id == owner_id)
    )
    return result.scalars().first()


async def get_by_ai_identity(
    session: AsyncSession, ai_identity_id: uuid.UUID
) -> ManagedBrain | None:
    result = await session.execute(
        select(ManagedBrain).where(ManagedBrain.ai_identity_id == ai_identity_id)
    )
    return result.scalars().first()


async def get_owned_brain(
    session: AsyncSession, *, brain_id: uuid.UUID, owner_id: uuid.UUID
) -> ManagedBrain | None:
    """Fetch a Brain only when the owner matches."""
    result = await session.execute(
        select(ManagedBrain).where(
            ManagedBrain.brain_id == brain_id,
            ManagedBrain.owner_id == owner_id,
        )
    )
    return result.scalars().first()


async def list_brains(
    session: AsyncSession,
    *,
    provisioning_status: str | None = None,
    llm_runtime_status: str | None = None,
    owner_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[ManagedBrain], int]:
    """Return a filtered, paginated page of registry rows."""
    filters = []
    if provisioning_status:
        filters.append(ManagedBrain.provisioning_status == provisioning_status)
    if llm_runtime_status:
        filters.append(ManagedBrain.llm_runtime_status == llm_runtime_status)
    if owner_id is not None:
        filters.append(ManagedBrain.owner_id == owner_id)

    base = select(ManagedBrain).where(*filters) if filters else select(ManagedBrain)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    result = await session.execute(
        base.order_by(ManagedBrain.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), total


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    """Return the number of Brains per provisioning status."""
    result = await session.execute(
        select(ManagedBrain.provisioning_status, func.count(ManagedBrain.brain_id)).group_by(
            ManagedBrain.provisioning_status
        )
    )
    return {str(status): int(count) for status, count in result.all()}


async def count_by_runtime_status(session: AsyncSession) -> dict[str, int]:
    """Return the number of Brains per LLM runtime status."""
    result = await session.execute(
        select(ManagedBrain.llm_runtime_status, func.count(ManagedBrain.brain_id)).group_by(
            ManagedBrain.llm_runtime_status
        )
    )
    return {str(status): int(count) for status, count in result.all()}


async def insert_reserved_brain(
    session: AsyncSession,
    *,
    brain_id: uuid.UUID,
    owner_id: uuid.UUID,
    ai_identity_id: uuid.UUID,
    provisioning_status: str,
    model_configuration: dict[str, Any],
    model_configuration_id: str,
    compute_allocation: dict[str, Any],
    storage_resource_reference: str,
    memory_resource_reference: str,
    vector_resource_reference: str,
    vault_resource_reference: str,
    resource_root: str,
    manifest_version: int = 1,
    llm_runtime_status: str = "unregistered",
    runtime_isolation: str | None = None,
) -> ManagedBrain:
    """Insert a reserved registry row.

    Uniqueness is enforced by the database (one primary Brain per owner and
    per AI identity). The caller is responsible for handling the resulting
    :class:`IntegrityError` as a concurrent-provisioning outcome.
    """
    brain = ManagedBrain(
        brain_id=brain_id,
        owner_id=owner_id,
        ai_identity_id=ai_identity_id,
        provisioning_status=provisioning_status,
        resource_manifest_version=manifest_version,
        manifest_version=manifest_version,
        model_configuration=model_configuration,
        model_configuration_id=model_configuration_id,
        compute_allocation=compute_allocation,
        storage_resource_reference=storage_resource_reference,
        memory_resource_reference=memory_resource_reference,
        vector_resource_reference=vector_resource_reference,
        vault_resource_reference=vault_resource_reference,
        resource_root=resource_root,
        llm_runtime_status=llm_runtime_status,
        runtime_isolation=runtime_isolation,
        connected_app_count=0,
        request_count=0,
    )
    session.add(brain)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise
    await session.refresh(brain)
    return brain
