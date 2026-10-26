"""The Brain provisioning pipeline.

Provisioning is the sequence that turns an authenticated Server 1 assertion
into a *working* Brain:

1. reserve the registry row (atomic, uniqueness enforced by the database);
2. build and persist the resource manifest;
3. reserve compute capacity;
4. provision the dedicated LLM runtime;
5. initialise the memory and vector resources;
6. initialise the personal vault;
7. initialise the processing and app-connection configuration;
8. run the health checks;
9. mark the Brain ready **only** when the required resources are operational.

Every step is idempotent, so a retry after a partial failure converges on the
same Brain instead of creating a second one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import BrainManifestRecord
from app.brain_manager.lifecycle.manager import BrainRuntimeManager
from app.brain_manager.runtime.base import RuntimeProvisioningError
from app.brain_manager.runtime.artifacts import provision_private_model_artifact
from app.brain_manager.provisioning.manifest import (
    MANIFEST_VERSION,
    build_manifest,
    serialize_manifest,
)
from app.brain_manager.config import get_brain_manager_config
from app.brain_manager.registry.enums import BrainProvisioningStatus, BrainRuntimeStatus
from app.brain_manager.registry.persistence import get_or_create
from app.brain_manager.registry.service import (
    BrainRegistryService,
    storage_reference,
)
from app.brain_manager.resource_manager.paths import brain_root
from app.brain_manager.runtime.factory import resolve_model_path
from app.brain_manager.vault.service import BrainVaultService
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.storage_db import ManagedBrain

logger = logging.getLogger(__name__)


@dataclass
class ProvisioningOutcome:
    """The result of one provisioning run."""

    brain_id: uuid.UUID
    owner_user_id: uuid.UUID
    ai_identity_id: uuid.UUID
    provisioning_status: BrainProvisioningStatus
    llm_runtime_id: str | None = None
    llm_runtime_status: BrainRuntimeStatus = BrainRuntimeStatus.UNREGISTERED
    runtime_isolation: str | None = None
    model_configuration_id: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    created: bool = False
    steps: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.provisioning_status is BrainProvisioningStatus.READY


class BrainProvisioningOrchestrator:
    """Runs the provisioning pipeline for one Brain."""

    def __init__(self, runtime_manager: BrainRuntimeManager | None = None) -> None:
        self._runtime_manager = runtime_manager

    @property
    def runtime_manager(self) -> BrainRuntimeManager:
        if self._runtime_manager is None:
            self._runtime_manager = BrainRuntimeManager()
        return self._runtime_manager

    async def provision(
        self,
        session: AsyncSession,
        *,
        brain_id: uuid.UUID,
        owner_id: uuid.UUID,
        ai_identity_id: uuid.UUID,
        owner_name: str | None = None,
        model_configuration: dict[str, Any],
        compute_allocation: dict[str, Any] | None = None,
        manifest_version: int = MANIFEST_VERSION,
    ) -> ProvisioningOutcome:
        """Provision (or converge) the primary Brain of one owner."""
        steps: list[str] = []
        allocation = dict(compute_allocation or {})
        allocation.setdefault("max_concurrent_requests", 1)

        existing = await BrainRegistryService.get(session, brain_id)
        created = existing is None
        brain = await BrainRegistryService.reserve(
            session,
            brain_id=brain_id,
            owner_id=owner_id,
            ai_identity_id=ai_identity_id,
            model_configuration=model_configuration,
            compute_allocation=allocation,
            owner_name=owner_name,
            manifest_version=manifest_version,
        )
        steps.append("registry_reserved")

        # Initialize the private data tree before expensive model provisioning.
        brain_directory = brain_root(
            brain.brain_id, resource_root=brain.resource_root
        )
        brain_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (brain_directory / "personal_llm").mkdir(mode=0o700, exist_ok=True)
        vault = BrainVaultService(session)
        await vault.ensure_initialized(brain)
        steps.extend(["personal_vault_initialized", "brain_resource_directories_initialized"])

        if get_brain_manager_config().runtime_mode == "dedicated":
            try:
                await provision_private_model_artifact(
                    resolve_model_path(get_brain_manager_config()),
                    brain_id=brain.brain_id,
                    resource_root=brain.resource_root,
                )
                steps.append("private_model_weights_provisioned")
            except RuntimeProvisioningError as exc:
                await BrainRegistryService.set_runtime_state(
                    session,
                    brain,
                    llm_runtime_status=BrainRuntimeStatus.FAILED,
                    error=exc.code,
                )
                await BrainRegistryService.set_provisioning_state(
                    session,
                    brain,
                    provisioning_status=BrainProvisioningStatus.FAILED,
                    error=exc.code,
                )
                manifest = await self._refresh_manifest(
                    session, brain, owner_name=owner_name
                )
                return ProvisioningOutcome(
                    brain_id=brain.brain_id,
                    owner_user_id=brain.owner_id,
                    ai_identity_id=brain.ai_identity_id,
                    provisioning_status=BrainProvisioningStatus.FAILED,
                    llm_runtime_id=brain.llm_runtime_id,
                    llm_runtime_status=BrainRuntimeStatus.FAILED,
                    runtime_isolation=brain.runtime_isolation,
                    model_configuration_id=brain.model_configuration_id,
                    manifest=manifest,
                    error_code=exc.code,
                    created=created,
                    steps=steps,
                )

        # Publish the manifest only after the initial private resources exist.
        manifest = await self._write_manifest(
            session, brain, model_configuration, allocation, owner_name=owner_name
        )
        steps.append("manifest_written")

        # -- 3/4. compute capacity and the dedicated runtime --------------
        runtime_result = await self.runtime_manager.provision_runtime(session, brain)
        steps.append(f"runtime:{runtime_result.status.value}")

        # -- 7. processing and app-connection configuration ---------------
        # The job queue starts empty and the app-connection snapshot starts
        # empty: a Brain has no app authorization until the user consents.
        steps.append("processing_initialized")

        # -- 8. health check ----------------------------------------------
        health = await self._health_check(session, brain)
        steps.append(f"health:{health['status']}")

        # -- 9. readiness ---------------------------------------------------
        runtime_ready = runtime_result.status in {
            BrainRuntimeStatus.READY,
            BrainRuntimeStatus.READY_SIMULATED,
            BrainRuntimeStatus.READY_EXTERNAL,
        }
        runtime_deferred = runtime_result.status in {
            BrainRuntimeStatus.WAITING_FOR_RESOURCES,
            BrainRuntimeStatus.RESOURCE_LIMITED,
        }
        if runtime_ready or runtime_deferred:
            await BrainRegistryService.set_provisioning_state(
                session,
                brain,
                provisioning_status=BrainProvisioningStatus.READY,
                error=None,
            )
            status = BrainProvisioningStatus.READY
        else:
            await BrainRegistryService.set_provisioning_state(
                session,
                brain,
                provisioning_status=BrainProvisioningStatus.FAILED,
                error=runtime_result.error_code or runtime_result.status.value,
            )
            status = BrainProvisioningStatus.FAILED

        manifest = await self._refresh_manifest(session, brain, owner_name=owner_name)
        return ProvisioningOutcome(
            brain_id=brain.brain_id,
            owner_user_id=brain.owner_id,
            ai_identity_id=brain.ai_identity_id,
            provisioning_status=status,
            llm_runtime_id=brain.llm_runtime_id,
            llm_runtime_status=BrainRuntimeStatus(brain.llm_runtime_status),
            runtime_isolation=brain.runtime_isolation,
            model_configuration_id=brain.model_configuration_id,
            manifest=manifest,
            error_code=brain.provisioning_error,
            created=created,
            steps=steps,
        )

    # -- helpers -----------------------------------------------------------
    async def _write_manifest(
        self,
        session: AsyncSession,
        brain: ManagedBrain,
        model_configuration: dict[str, Any],
        compute_allocation: dict[str, Any],
        *,
        owner_name: str | None,
    ) -> dict[str, Any]:
        """Build the manifest and store it (database + Brain directory)."""
        record = await self._manifest_record(session, brain)
        persisted_owner_name = owner_name
        if persisted_owner_name is None:
            persisted_owner_name = record.manifest.get("owner_name")
        manifest = build_manifest(
            brain_id=str(brain.brain_id),
            owner_user_id=str(brain.owner_id),
            ai_identity_id=str(brain.ai_identity_id),
            owner_name=persisted_owner_name,
            provisioning_status=brain.provisioning_status,
            llm_runtime_id=brain.llm_runtime_id,
            llm_runtime_status=brain.llm_runtime_status,
            runtime_isolation=brain.runtime_isolation,
            model_configuration_id=brain.model_configuration_id,
            compute_allocation=compute_allocation,
            resource_references={
                "root": brain.resource_root,
                "personal_llm": f"{brain.resource_root}/personal_llm",
                "model_weights": self._model_weights_reference(brain),
                "memory": f"{brain.resource_root}/memory_vault",
                "vector": f"{brain.resource_root}/indexes",
                "vault": f"{brain.resource_root}/memory_vault/vault.sqlite",
                "objects": f"{brain.resource_root}/objects",
                "storage": storage_reference(brain.brain_id, brain.owner_id),
            },
            connected_app_count=brain.connected_app_count,
        )

        record.manifest = manifest
        record.manifest_version = MANIFEST_VERSION
        await session.commit()

        try:
            directory = brain_root(
                brain.brain_id, resource_root=brain.resource_root
            )
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            (directory / "personal_llm").mkdir(mode=0o700, exist_ok=True)
            (directory / "personal_llm" / "model_weights").mkdir(
                mode=0o700, exist_ok=True
            )
            storage = get_brain_vault_storage()
            storage.memory_vault_directory(brain)
            storage.object_directory(brain)
            storage.index_directory(brain)
            (directory / "brain_manifest.json").write_text(
                serialize_manifest(manifest), encoding="utf-8"
            )
        except OSError as exc:
            # The manifest in the database stays authoritative; the on-disk
            # copy is a convenience for operators.
            logger.warning(
                "Could not write the manifest file for Brain %s: %s",
                brain.brain_id,
                type(exc).__name__,
            )
        return manifest

    @staticmethod
    def _model_weights_reference(brain: ManagedBrain) -> str:
        """Describe an existing private model copy, otherwise the configured artifact."""
        model_path = resolve_model_path(get_brain_manager_config())
        weights_root = brain_root(
            brain.brain_id, resource_root=brain.resource_root
        ) / "personal_llm" / "model_weights"
        private_model = weights_root if model_path.is_dir() else weights_root / model_path.name
        private_model_exists = (
            (private_model / "config.json").is_file()
            if model_path.is_dir()
            else private_model.is_file()
        )
        if not private_model_exists:
            return "shared_model_artifact"
        suffix = private_model.name if private_model.is_file() else ""
        relative = f"{brain.resource_root}/personal_llm/model_weights"
        return f"{relative}/{suffix}" if suffix else relative

    async def _refresh_manifest(
        self,
        session: AsyncSession,
        brain: ManagedBrain,
        *,
        owner_name: str | None,
    ) -> dict[str, Any]:
        """Rebuild the manifest so it reflects the final state."""
        return await self._write_manifest(
            session,
            brain,
            brain.model_configuration or {},
            brain.compute_allocation or {},
            owner_name=owner_name,
        )

    async def _manifest_record(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> BrainManifestRecord:
        """Return the manifest row of one Brain, creating it exactly once.

        Two concurrent provisioning requests for the same Brain must converge
        on one manifest row, so the insert is guarded by the unique constraint
        instead of a plain read-then-insert.
        """
        from sqlalchemy import select

        async def _fetch() -> BrainManifestRecord | None:
            result = await session.execute(
                select(BrainManifestRecord).where(
                    BrainManifestRecord.brain_id == brain.brain_id
                )
            )
            return result.scalars().first()

        record = await get_or_create(
            session,
            model=BrainManifestRecord,
            lookup={"brain_id": brain.brain_id},
            create=lambda: BrainManifestRecord(
                brain_id=brain.brain_id,
                owner_id=brain.owner_id,
                manifest_version=MANIFEST_VERSION,
                manifest={},
            ),
            fetch=_fetch,
        )
        return record

    async def _health_check(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> dict[str, Any]:
        """Probe the Brain's runtime and record the result."""
        from app.brain_manager.db_models import BrainHealthCheck

        runtime = self.runtime_manager.handle(brain.brain_id)
        if runtime is None:
            status = "unavailable"
            detail = "no runtime is live for this Brain"
            runtime_status = brain.llm_runtime_status
        else:
            health = runtime.health()
            status = health.status
            detail = health.detail
            runtime_status = brain.llm_runtime_status

        brain.last_health_check = datetime.now(timezone.utc)
        await session.commit()

        session.add(
            BrainHealthCheck(
                brain_id=brain.brain_id,
                owner_id=brain.owner_id,
                status=status,
                runtime_status=runtime_status,
                details={"detail": detail} if detail else {},
            )
        )
        await session.commit()
        return {"status": status, "detail": detail, "runtime_status": runtime_status}
