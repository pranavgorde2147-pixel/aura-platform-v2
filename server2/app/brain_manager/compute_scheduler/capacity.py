"""Compute capacity accounting for dedicated Brain runtimes.

Physical compute is finite. Before a runtime is started the manager verifies
that the deployment still has room for it. When it does not, the Brain is
marked ``waiting_for_resources`` or ``resource_limited`` - it is never quietly
served by somebody else's runtime.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.registry.enums import ResourceAvailability
from app.storage_db import ManagedBrain

#: Runtime statuses that hold a compute slot.
ACTIVE_RUNTIME_STATUSES = (
    "starting",
    "ready",
    "ready_simulated",
    "ready_external",
    "degraded",
    "provisioning",
)


@dataclass(frozen=True)
class CapacitySnapshot:
    """The current compute capacity picture."""

    max_runtimes: int
    active_runtimes: int
    available_runtimes: int
    max_concurrent_requests_per_brain: int

    @property
    def utilization(self) -> float:
        """Return the share of the runtime slots that is in use."""
        if self.max_runtimes <= 0:
            return 0.0
        return round(self.active_runtimes / self.max_runtimes, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_runtimes": self.max_runtimes,
            "active_runtimes": self.active_runtimes,
            "available_runtimes": self.available_runtimes,
            "max_concurrent_requests_per_brain": self.max_concurrent_requests_per_brain,
            "utilization": self.utilization,
        }


class ComputeCapacityManager:
    """Tracks and reserves runtime slots for dedicated Brain runtimes.

    The reservation is atomic inside the process (an ``asyncio.Lock``) and the
    occupancy is additionally derived from the registry, so a restarted process
    does not hand out slots that are already in use.
    """

    def __init__(self, *, max_runtimes: int, max_concurrent_requests_per_brain: int = 1) -> None:
        self.max_runtimes = max(0, int(max_runtimes))
        self.max_concurrent_requests_per_brain = max(1, int(max_concurrent_requests_per_brain))
        self._lock = asyncio.Lock()
        self._reserved: set[uuid.UUID] = set()
        self._released: set[uuid.UUID] = set()

    async def reserve(
        self, session: AsyncSession, brain_id: uuid.UUID
    ) -> tuple[ResourceAvailability, str]:
        """Reserve one runtime slot for ``brain_id``.

        Returns the availability and a human-readable reason. Re-reserving a
        Brain that already holds a slot is idempotent.
        """
        async with self._lock:
            if brain_id in self._reserved:
                return ResourceAvailability.AVAILABLE, "slot already reserved"

            active = await self._count_active(session)
            if active >= self.max_runtimes:
                return (
                    ResourceAvailability.WAITING,
                    f"all {self.max_runtimes} runtime slots are in use",
                )
            self._reserved.add(brain_id)
            return ResourceAvailability.AVAILABLE, "slot reserved"

    async def release(self, brain_id: uuid.UUID) -> None:
        """Release the slot held by ``brain_id``."""
        async with self._lock:
            self._reserved.discard(brain_id)
            self._released.add(brain_id)

    async def _count_active(self, session: AsyncSession) -> int:
        """Count the Brains that currently hold a runtime slot."""
        total = await session.scalar(
            select(func.count(ManagedBrain.brain_id)).where(
                ManagedBrain.llm_runtime_status.in_(ACTIVE_RUNTIME_STATUSES),
                ManagedBrain.llm_runtime_id.is_not(None),
            )
        )
        return int(total or 0)

    async def snapshot(self, session: AsyncSession) -> CapacitySnapshot:
        """Return the current capacity picture."""
        active = await self._count_active(session)
        return CapacitySnapshot(
            max_runtimes=self.max_runtimes,
            active_runtimes=active,
            available_runtimes=max(0, self.max_runtimes - active),
            max_concurrent_requests_per_brain=self.max_concurrent_requests_per_brain,
        )

    def has_local_reservation(self, brain_id: uuid.UUID) -> bool:
        """Return True when this process holds a reservation for the Brain."""
        return brain_id in self._reserved
