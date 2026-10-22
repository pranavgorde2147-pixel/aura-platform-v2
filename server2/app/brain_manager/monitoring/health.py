"""Health checks for provisioned Brains.

A health check never mutates the Brain: it only *reads* the registry, asks the
Brain's runtime for its status and inspects the Brain's own resources. The
result is written to ``brain_health_checks`` so the history of a Brain can be
audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import BrainHealthCheck, BrainProcessingJob
from app.brain_manager.lifecycle.manager import BrainRuntimeManager
from app.brain_manager.registry.enums import BrainProvisioningStatus, BrainRuntimeStatus
from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.vault.service import BrainVaultService
from app.storage_db import ManagedBrain

#: The overall verdict of a health check.
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_UNKNOWN = "unknown"


@dataclass
class BrainHealthReport:
    """The health of one Brain."""

    brain_id: uuid.UUID
    owner_id: uuid.UUID
    status: str
    provisioning_status: str
    runtime_status: str
    runtime_id: str | None = None
    runtime_isolation: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "brain_id": str(self.brain_id),
            "owner_id": str(self.owner_id),
            "status": self.status,
            "provisioning_status": self.provisioning_status,
            "runtime_status": self.runtime_status,
            "runtime_id": self.runtime_id,
            "runtime_isolation": self.runtime_isolation,
            "checks": self.checks,
            "issues": self.issues,
            "checked_at": self.checked_at.isoformat(),
        }


class BrainHealthMonitor:
    """Runs the health checks of one Brain and of the whole cluster."""

    def __init__(self, runtime_manager: BrainRuntimeManager | None = None) -> None:
        self._runtime_manager = runtime_manager

    @property
    def runtime_manager(self) -> BrainRuntimeManager:
        if self._runtime_manager is None:
            self._runtime_manager = BrainRuntimeManager()
        return self._runtime_manager

    async def check_brain(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> BrainHealthReport:
        """Check one Brain and record the result."""
        issues: list[str] = []
        checks: dict[str, Any] = {}

        # -- registry ------------------------------------------------------
        provisioning_status = brain.provisioning_status
        checks["registry"] = {
            "provisioning_status": provisioning_status,
            "model_configuration_id": brain.model_configuration_id,
            "manifest_version": brain.manifest_version,
            "request_count": brain.request_count,
        }

        # -- runtime -------------------------------------------------------
        runtime = self.runtime_manager.handle(brain.brain_id)
        if runtime is None:
            runtime_status = brain.llm_runtime_status
            checks["runtime"] = {
                "status": "missing",
                "runtime_id": brain.llm_runtime_id,
                "isolation": brain.runtime_isolation,
            }
            if brain.llm_runtime_status in {
                BrainRuntimeStatus.READY.value,
                BrainRuntimeStatus.READY_EXTERNAL.value,
                BrainRuntimeStatus.READY_SIMULATED.value,
            }:
                issues.append("the registry claims a live runtime but none is loaded")
        else:
            runtime_health = runtime.health()
            runtime_status = brain.llm_runtime_status
            checks["runtime"] = {
                "status": runtime_health.status,
                "detail": runtime_health.detail,
                "runtime_id": runtime.runtime_id,
                "isolation": runtime.isolation,
            }
            if not runtime_health.healthy:
                issues.append(
                    f"the runtime reports '{runtime_health.status}'"
                )
            if runtime.runtime_id != brain.llm_runtime_id:
                issues.append("the runtime id does not match the registry")

        # -- vault and vector index ----------------------------------------
        vault = BrainVaultService(session)
        vector_status = await vault.vector_status(brain)
        checks["vector"] = vector_status
        if vector_status["status"] not in {"ready", "degraded"}:
            issues.append("the vector index is not ready")
        record_count = await vault.count_records(brain)
        checks["vault"] = {"record_count": record_count}

        # -- processing ----------------------------------------------------
        job_rows = await session.execute(
            select(BrainProcessingJob.status, func.count(BrainProcessingJob.id))
            .where(
                BrainProcessingJob.brain_id == brain.brain_id,
                BrainProcessingJob.owner_id == brain.owner_id,
            )
            .group_by(BrainProcessingJob.status)
        )
        job_counts = {str(status): int(count) for status, count in job_rows.all()}
        checks["processing"] = {"job_counts": job_counts}
        dead = job_counts.get("dead", 0)
        if dead:
            issues.append(f"{dead} processing job(s) exhausted their attempts")

        # -- verdict --------------------------------------------------------
        if not issues:
            status = HEALTH_HEALTHY
        elif provisioning_status == BrainProvisioningStatus.FAILED.value:
            status = HEALTH_UNHEALTHY
        else:
            status = HEALTH_DEGRADED

        report = BrainHealthReport(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            status=status,
            provisioning_status=provisioning_status,
            runtime_status=runtime_status,
            runtime_id=brain.llm_runtime_id,
            runtime_isolation=brain.runtime_isolation,
            checks=checks,
            issues=issues,
        )

        brain.last_health_check = report.checked_at
        await session.commit()
        session.add(
            BrainHealthCheck(
                brain_id=brain.brain_id,
                owner_id=brain.owner_id,
                status=status,
                runtime_status=runtime_status,
                details={"issues": issues, "checks": checks},
            )
        )
        await session.commit()
        return report

    async def check_all(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[BrainHealthReport]:
        """Check every registered Brain."""
        result = await session.execute(
            select(ManagedBrain).order_by(ManagedBrain.created_at.asc()).limit(limit)
        )
        brains = list(result.scalars().all())
        reports: list[BrainHealthReport] = []
        for brain in brains:
            reports.append(await self.check_brain(session, brain))
        return reports

    async def cluster_status(self, session: AsyncSession) -> dict[str, Any]:
        """Return an aggregated view of the whole Brain cluster."""
        result = await session.execute(
            select(
                ManagedBrain.provisioning_status,
                ManagedBrain.llm_runtime_status,
                func.count(ManagedBrain.brain_id),
            ).group_by(
                ManagedBrain.provisioning_status, ManagedBrain.llm_runtime_status
            )
        )
        provisioning: dict[str, int] = {}
        runtimes: dict[str, int] = {}
        for provisioning_status, runtime_status, count in result.all():
            provisioning[str(provisioning_status)] = (
                provisioning.get(str(provisioning_status), 0) + int(count)
            )
            runtimes[str(runtime_status)] = runtimes.get(str(runtime_status), 0) + int(count)

        total = int(
            await session.scalar(select(func.count(ManagedBrain.brain_id))) or 0
        )
        capacity = await self.runtime_manager.capacity.snapshot(session)
        return {
            "total_brains": total,
            "provisioning_status_counts": provisioning,
            "runtime_status_counts": runtimes,
            "live_runtimes": len(self.runtime_manager.active_brain_ids()),
            "capacity": capacity.to_dict(),
        }


async def get_owned_brain_or_404(
    session: AsyncSession, *, brain_id: uuid.UUID, owner_id: uuid.UUID
) -> ManagedBrain | None:
    """Return the Brain of one owner, or ``None``."""
    return await BrainRegistryService.get_owned(
        session, brain_id=brain_id, owner_id=owner_id
    )


__all__ = [
    "BrainHealthMonitor",
    "BrainHealthReport",
    "HEALTH_DEGRADED",
    "HEALTH_HEALTHY",
    "HEALTH_UNHEALTHY",
    "HEALTH_UNKNOWN",
]
