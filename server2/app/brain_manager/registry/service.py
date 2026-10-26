"""The Brain registry service.

The registry is the authoritative source for *who owns which Brain* and for the
state of that Brain's resources. Two guarantees matter:

**Uniqueness** - a user has at most one primary Brain and an AI identity has
at most one primary Brain. The database enforces both with unique
constraints, so two concurrent provisioning requests can never create two
primary Brains for the same owner.

**Idempotency** - registering the same ``(brain_id, owner_id,
ai_identity_id)`` twice returns the same registry entry and never duplicates
runtime capacity.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.registry import repository
from app.brain_manager.registry.enums import (
    BrainProvisioningStatus,
    BrainRuntimeStatus,
    ResourceAvailability,
)
from app.brain_manager.registry.models import BrainRegistryEntry
from app.brain_manager.resource_manager.paths import brain_relative_path
from app.storage_db import ManagedBrain


class BrainRegistryConflict(Exception):
    """Raised when a Brain identity is already bound to different resources."""


class BrainCapacityUnavailable(Exception):
    """Raised when no compute capacity is available for a new runtime."""

    def __init__(self, availability: ResourceAvailability, message: str) -> None:
        super().__init__(message)
        self.availability = availability
        self.message = message


def memory_reference(brain_id: uuid.UUID) -> str:
    return brain_relative_path(brain_id, "personal_memory")


def vector_reference(brain_id: uuid.UUID) -> str:
    return brain_relative_path(brain_id, "indexes")


def vault_reference(brain_id: uuid.UUID) -> str:
    return brain_relative_path(brain_id, "vault.sqlite")


def storage_reference(brain_id: uuid.UUID, owner_id: uuid.UUID) -> str:
    """Return the object-store prefix that belongs to one Brain."""
    return f"owners/{owner_id}/brains/{brain_id}"


def model_configuration_id(model_configuration: dict[str, Any]) -> str:
    """Return the stable identifier of a model configuration."""
    model_id = str(model_configuration.get("model_id") or "unknown")
    revision = str(model_configuration.get("revision") or "1")
    return f"{model_id}@{revision}"


def to_entry(brain: ManagedBrain) -> BrainRegistryEntry:
    """Map a registry row onto its transport representation."""
    return BrainRegistryEntry(
        brain_id=brain.brain_id,
        owner_user_id=brain.owner_id,
        ai_identity_id=brain.ai_identity_id,
        provisioning_status=BrainProvisioningStatus(brain.provisioning_status),
        llm_runtime_id=brain.llm_runtime_id,
        llm_runtime_status=BrainRuntimeStatus(brain.llm_runtime_status),
        runtime_isolation=brain.runtime_isolation,
        model_configuration_id=brain.model_configuration_id,
        compute_allocation=brain.compute_allocation or {},
        memory_resource_reference=brain.memory_resource_reference,
        vector_resource_reference=brain.vector_resource_reference,
        vault_resource_reference=brain.vault_resource_reference,
        resource_root=brain.resource_root,
        manifest_version=brain.manifest_version,
        connected_app_count=brain.connected_app_count,
        request_count=brain.request_count,
        created_at=brain.created_at,
        updated_at=brain.updated_at,
        last_health_check=brain.last_health_check,
        provisioning_error=brain.provisioning_error,
        runtime_last_error=brain.runtime_last_error,
    )


class BrainRegistryService:
    """Read/write operations on the Brain registry."""

    @staticmethod
    async def get(session: AsyncSession, brain_id: uuid.UUID) -> ManagedBrain | None:
        return await repository.get_by_brain_id(session, brain_id)

    @staticmethod
    async def get_owned(
        session: AsyncSession, *, brain_id: uuid.UUID, owner_id: uuid.UUID
    ) -> ManagedBrain | None:
        return await repository.get_owned_brain(
            session, brain_id=brain_id, owner_id=owner_id
        )

    @staticmethod
    async def get_by_owner(session: AsyncSession, owner_id: uuid.UUID) -> ManagedBrain | None:
        return await repository.get_by_owner(session, owner_id)

    @staticmethod
    async def get_by_ai_identity(
        session: AsyncSession, ai_identity_id: uuid.UUID
    ) -> ManagedBrain | None:
        return await repository.get_by_ai_identity(session, ai_identity_id)

    @staticmethod
    async def reserve(
        session: AsyncSession,
        *,
        brain_id: uuid.UUID,
        owner_id: uuid.UUID,
        ai_identity_id: uuid.UUID,
        model_configuration: dict[str, Any],
        compute_allocation: dict[str, Any],
        owner_name: str | None = None,
        manifest_version: int = 1,
    ) -> ManagedBrain:
        """Reserve (or return) the primary Brain of one owner.

        The reservation is atomic: the insert either wins or loses the unique
        constraint. A lost race resolves to the winner's row when the winner
        describes the same Brain, and to a conflict otherwise.
        """
        existing = await repository.get_by_brain_id(session, brain_id)
        if existing is not None:
            BrainRegistryService._assert_same_binding(existing, owner_id, ai_identity_id)
            return existing

        owner_brain = await repository.get_by_owner(session, owner_id)
        if owner_brain is not None:
            if owner_brain.brain_id == brain_id:
                BrainRegistryService._assert_same_binding(
                    owner_brain, owner_id, ai_identity_id
                )
                return owner_brain
            raise BrainRegistryConflict(
                "The owner already has a primary Brain with a different identity"
            )

        identity_brain = await repository.get_by_ai_identity(session, ai_identity_id)
        if identity_brain is not None:
            if identity_brain.brain_id == brain_id:
                BrainRegistryService._assert_same_binding(
                    identity_brain, owner_id, ai_identity_id
                )
                return identity_brain
            raise BrainRegistryConflict(
                "The AI identity already has a primary Brain with a different identity"
            )

        try:
            return await repository.insert_reserved_brain(
                session,
                brain_id=brain_id,
                owner_id=owner_id,
                ai_identity_id=ai_identity_id,
                provisioning_status=BrainProvisioningStatus.PROVISIONING.value,
                model_configuration=model_configuration,
                model_configuration_id=model_configuration_id(model_configuration),
                compute_allocation=compute_allocation,
                storage_resource_reference=storage_reference(brain_id, owner_id),
                memory_resource_reference=brain_relative_path(
                    brain_id, "personal_memory", owner_name=owner_name
                ),
                vector_resource_reference=vector_reference(brain_id),
                vault_resource_reference=vault_reference(brain_id),
                resource_root=brain_relative_path(brain_id, owner_name=owner_name),
                manifest_version=manifest_version,
                llm_runtime_status=BrainRuntimeStatus.UNREGISTERED.value,
            )
        except IntegrityError as exc:
            # A concurrent request won the reservation.
            concurrent = await repository.get_by_brain_id(session, brain_id)
            if concurrent is not None:
                BrainRegistryService._assert_same_binding(
                    concurrent, owner_id, ai_identity_id
                )
                return concurrent
            raise BrainRegistryConflict(
                "The owner or AI identity already has a primary Brain"
            ) from exc

    @staticmethod
    def _assert_same_binding(
        brain: ManagedBrain, owner_id: uuid.UUID, ai_identity_id: uuid.UUID
    ) -> None:
        if brain.owner_id != owner_id or brain.ai_identity_id != ai_identity_id:
            raise BrainRegistryConflict(
                "The Brain identity is already bound to a different owner or AI identity"
            )

    @staticmethod
    async def set_runtime_state(
        session: AsyncSession,
        brain: ManagedBrain,
        *,
        llm_runtime_status: BrainRuntimeStatus,
        llm_runtime_id: str | None = None,
        runtime_isolation: str | None = None,
        error: str | None = None,
    ) -> ManagedBrain:
        """Record the runtime state of one Brain."""
        brain.llm_runtime_status = llm_runtime_status.value
        if llm_runtime_id is not None:
            brain.llm_runtime_id = llm_runtime_id
        if runtime_isolation is not None:
            brain.runtime_isolation = runtime_isolation
        brain.runtime_last_error = error
        await session.commit()
        return brain

    @staticmethod
    async def set_provisioning_state(
        session: AsyncSession,
        brain: ManagedBrain,
        *,
        provisioning_status: BrainProvisioningStatus,
        error: str | None = None,
    ) -> ManagedBrain:
        """Record the provisioning state of one Brain."""
        brain.provisioning_status = provisioning_status.value
        brain.provisioning_error = error
        await session.commit()
        return brain

    @staticmethod
    async def record_request(session: AsyncSession, brain: ManagedBrain) -> None:
        """Record one inference request against a Brain."""
        brain.request_count = int(brain.request_count or 0) + 1
        await session.commit()
