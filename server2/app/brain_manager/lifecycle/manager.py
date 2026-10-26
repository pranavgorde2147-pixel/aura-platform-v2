"""Lifecycle management for dedicated per-Brain LLM runtimes.

This module owns the mapping ``brain_id -> runtime``. It is the only place that
creates, stops, restarts and releases a runtime, and it is the only place
that writes the runtime state back to the registry.

Guarantees:

* one Brain never shares a runtime with another Brain;
* every runtime has a unique identifier that is recorded in the registry;
* a failed provisioning attempt is reported as a failure - never silently
  replaced by a shared runtime;
* capacity is verified *before* a runtime is created.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.compute_scheduler.capacity import ComputeCapacityManager
from app.brain_manager.config import get_brain_manager_config
from app.brain_manager.registry.enums import BrainRuntimeStatus, ResourceAvailability
from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.runtime.base import (
    BrainRuntimeHandle,
    RuntimeContext,
    RuntimeProvisioningError,
)
from app.brain_manager.runtime.factory import get_runtime_adapter
from app.storage_db import ManagedBrain

logger = logging.getLogger(__name__)

#: Runtime identifier prefix. The identifier is unique across all Brains.
RUNTIME_ID_PREFIX = "rt_"

#: How a runtime status maps from the adapter's isolation level.
_ISOLATION_STATUS = {
    "dedicated_context": BrainRuntimeStatus.READY,
    "dedicated_runtime_shared_weights": BrainRuntimeStatus.READY,
    "dedicated_runtime_per_brain_weights": BrainRuntimeStatus.READY,
    "dedicated_weights": BrainRuntimeStatus.READY,
    "context_isolated_shared_weights": BrainRuntimeStatus.READY_EXTERNAL,
    "simulated": BrainRuntimeStatus.READY_SIMULATED,
}


def new_runtime_id() -> str:
    """Return a fresh, globally unique runtime identifier."""
    return f"{RUNTIME_ID_PREFIX}{uuid.uuid4().hex}"


@dataclass
class RuntimeProvisionResult:
    """The outcome of a runtime provisioning attempt."""

    brain_id: uuid.UUID
    runtime_id: str | None
    status: BrainRuntimeStatus
    isolation: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status in {
            BrainRuntimeStatus.READY,
            BrainRuntimeStatus.READY_SIMULATED,
            BrainRuntimeStatus.READY_EXTERNAL,
        }


class BrainRuntimeManager:
    """Creates, tracks and releases the dedicated runtime of every Brain."""

    def __init__(
        self,
        *,
        adapter: Any | None = None,
        capacity: ComputeCapacityManager | None = None,
    ) -> None:
        self._adapter = adapter
        self._capacity = capacity
        self._runtimes: dict[uuid.UUID, BrainRuntimeHandle] = {}
        self._slots: dict[uuid.UUID, asyncio.Semaphore] = {}
        self._allocation_lock = asyncio.Lock()
        self._active_requests: dict[uuid.UUID, int] = {}
        self._runtime_last_used: dict[uuid.UUID, float] = {}
        self._known_brains: dict[uuid.UUID, ManagedBrain] = {}

    # -- accessors ---------------------------------------------------------
    @property
    def adapter(self) -> Any:
        if self._adapter is None:
            self._adapter = get_runtime_adapter()
        return self._adapter

    @property
    def capacity(self) -> ComputeCapacityManager:
        config = get_brain_manager_config()
        if self._capacity is None or (
            self._capacity.max_runtimes != config.max_concurrent_runtimes
            or self._capacity.max_concurrent_requests_per_brain
            != config.max_concurrent_requests_per_brain
        ):
            self._capacity = ComputeCapacityManager(
                max_runtimes=config.max_concurrent_runtimes,
                max_concurrent_requests_per_brain=config.max_concurrent_requests_per_brain,
            )
        return self._capacity

    def handle(self, brain_id: uuid.UUID) -> BrainRuntimeHandle | None:
        """Return the live runtime of one Brain, if any."""
        return self._runtimes.get(brain_id)

    def context(self, brain_id: uuid.UUID) -> RuntimeContext | None:
        """Return the per-Brain inference context, if a runtime exists."""
        runtime = self._runtimes.get(brain_id)
        return runtime.context if runtime is not None else None

    def active_brain_ids(self) -> list[uuid.UUID]:
        """Return the Brains that currently own a live runtime."""
        return list(self._runtimes)

    def slot(self, brain_id: uuid.UUID, *, limit: int | None = None) -> asyncio.Semaphore:
        """Return the per-Brain request semaphore."""
        slot = self._slots.get(brain_id)
        if slot is None:
            slot = asyncio.Semaphore(
                limit or self.capacity.max_concurrent_requests_per_brain
            )
            self._slots[brain_id] = slot
        return slot

    async def request_started(self, brain_id: uuid.UUID) -> None:
        """Pin a Brain runtime while one of its requests is being served."""
        async with self._allocation_lock:
            self._active_requests[brain_id] = self._active_requests.get(brain_id, 0) + 1
            self._runtime_last_used[brain_id] = time.monotonic()

    async def request_finished(self, brain_id: uuid.UUID) -> None:
        """Unpin a Brain runtime after one of its requests has completed."""
        async with self._allocation_lock:
            active = self._active_requests.get(brain_id, 0)
            if active <= 1:
                self._active_requests.pop(brain_id, None)
            else:
                self._active_requests[brain_id] = active - 1
            self._runtime_last_used[brain_id] = time.monotonic()

    async def _reserve_runtime_slot(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> tuple[ResourceAvailability, str]:
        """Reserve capacity, evicting the least-recently-used idle local runtime."""
        availability, reason = await self.capacity.reserve(session, brain.brain_id)
        if availability is ResourceAvailability.AVAILABLE:
            return availability, reason

        candidates = sorted(
            (
                runtime_brain_id
                for runtime_brain_id in self._runtimes
                if runtime_brain_id != brain.brain_id
                and self._active_requests.get(runtime_brain_id, 0) == 0
                and runtime_brain_id in self._known_brains
            ),
            key=lambda runtime_brain_id: self._runtime_last_used.get(
                runtime_brain_id, 0.0
            ),
        )
        for candidate_id in candidates:
            candidate_brain = await BrainRegistryService.get(session, candidate_id)
            if candidate_brain is None:
                await self._release_handle(candidate_id)
                await self.capacity.release(candidate_id)
                continue
            self._known_brains[candidate_id] = candidate_brain
            await self._release_handle(candidate_id)
            await self.capacity.release(candidate_id)
            await BrainRegistryService.set_runtime_state(
                session,
                candidate_brain,
                llm_runtime_status=BrainRuntimeStatus.STOPPED,
                error=None,
            )
            availability, reason = await self.capacity.reserve(
                session, brain.brain_id
            )
            if availability is ResourceAvailability.AVAILABLE:
                logger.info("Evicted idle Brain runtime %s to serve another Brain", candidate_id)
                return availability, reason

        return availability, reason

    # -- lifecycle ---------------------------------------------------------
    async def provision_runtime(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> RuntimeProvisionResult:
        """Create (or return) the dedicated runtime of one Brain."""
        self._known_brains[brain.brain_id] = brain
        async with self._allocation_lock:
            existing = self._runtimes.get(brain.brain_id)
            if existing is not None:
                health = existing.health()
                if health.healthy:
                    # The runtime is already live for this Brain. The registry
                    # row is re-synced so that a caller that loaded the row
                    # before the runtime existed still sees the runtime id and
                    # its isolation - the row is the shared source of truth.
                    status = _ISOLATION_STATUS.get(
                        existing.isolation, BrainRuntimeStatus.READY
                    )
                    await BrainRegistryService.set_runtime_state(
                        session,
                        brain,
                        llm_runtime_status=status,
                        llm_runtime_id=existing.runtime_id,
                        runtime_isolation=existing.isolation,
                        error=None,
                    )
                    self._runtime_last_used[brain.brain_id] = time.monotonic()
                    return RuntimeProvisionResult(
                        brain_id=brain.brain_id,
                        runtime_id=existing.runtime_id,
                        status=status,
                        isolation=existing.isolation,
                        metadata=existing.describe(),
                    )
                # A broken runtime is replaced rather than reused.
                await self._release_handle(brain.brain_id)

            availability, reason = await self._reserve_runtime_slot(session, brain)
            if availability is not ResourceAvailability.AVAILABLE:
                status = (
                    BrainRuntimeStatus.RESOURCE_LIMITED
                    if availability is ResourceAvailability.LIMITED
                    else BrainRuntimeStatus.WAITING_FOR_RESOURCES
                )
                brain.llm_runtime_status = status.value
                brain.runtime_last_error = f"capacity_unavailable: {reason}"
                await session.commit()
                return RuntimeProvisionResult(
                    brain_id=brain.brain_id,
                    runtime_id=None,
                    status=status,
                    error_code="capacity_unavailable",
                    error_message=reason,
                )

            await BrainRegistryService.set_runtime_state(
                session,
                brain,
                llm_runtime_status=BrainRuntimeStatus.STARTING,
            )

            runtime_id = new_runtime_id()
            try:
                resource_root = getattr(brain, "resource_root", None)
                if resource_root and resource_root != f"brain_{brain.brain_id}":
                    runtime = self.adapter.create(
                        brain_id=brain.brain_id,
                        runtime_id=runtime_id,
                        resource_root=resource_root,
                    )
                else:
                    runtime = self.adapter.create(
                        brain_id=brain.brain_id, runtime_id=runtime_id
                    )
            except RuntimeProvisioningError as exc:
                await self.capacity.release(brain.brain_id)
                brain.llm_runtime_status = BrainRuntimeStatus.FAILED.value
                brain.runtime_last_error = exc.code
                brain.provisioning_error = exc.code
                await session.commit()
                logger.warning(
                    "Dedicated runtime provisioning failed for Brain %s: %s",
                    brain.brain_id,
                    exc.code,
                )
                return RuntimeProvisionResult(
                    brain_id=brain.brain_id,
                    runtime_id=None,
                    status=BrainRuntimeStatus.FAILED,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except Exception as exc:  # pragma: no cover - defensive
                await self.capacity.release(brain.brain_id)
                brain.llm_runtime_status = BrainRuntimeStatus.FAILED.value
                brain.runtime_last_error = "runtime_creation_failed"
                brain.provisioning_error = "runtime_creation_failed"
                await session.commit()
                logger.error(
                    "Unexpected runtime creation failure for Brain %s: %s",
                    brain.brain_id,
                    type(exc).__name__,
                )
                return RuntimeProvisionResult(
                    brain_id=brain.brain_id,
                    runtime_id=None,
                    status=BrainRuntimeStatus.FAILED,
                    error_code="runtime_creation_failed",
                    error_message=type(exc).__name__,
                )

            self._runtimes[brain.brain_id] = runtime
            self._runtime_last_used[brain.brain_id] = time.monotonic()
            status = _ISOLATION_STATUS.get(runtime.isolation, BrainRuntimeStatus.READY)
            await BrainRegistryService.set_runtime_state(
                session,
                brain,
                llm_runtime_status=status,
                llm_runtime_id=runtime.runtime_id,
                runtime_isolation=runtime.isolation,
                error=None,
            )
            brain.runtime_started_at = brain.updated_at
            await session.commit()
            return RuntimeProvisionResult(
                brain_id=brain.brain_id,
                runtime_id=runtime.runtime_id,
                status=status,
                isolation=runtime.isolation,
                metadata=runtime.describe(),
            )

    async def stop_runtime(self, session: AsyncSession, brain: ManagedBrain) -> bool:
        """Stop the runtime of one Brain and release its resources."""
        async with self._allocation_lock:
            released = await self._release_handle(brain.brain_id)
            await self.capacity.release(brain.brain_id)
            await BrainRegistryService.set_runtime_state(
                session,
                brain,
                llm_runtime_status=BrainRuntimeStatus.STOPPED,
            )
            return released

    async def restart_runtime(
        self, session: AsyncSession, brain: ManagedBrain
    ) -> RuntimeProvisionResult:
        """Stop and re-create the runtime of one Brain (recovery path)."""
        await self.stop_runtime(session, brain)
        return await self.provision_runtime(session, brain)

    async def _release_handle(self, brain_id: uuid.UUID) -> bool:
        runtime = self._runtimes.pop(brain_id, None)
        if runtime is None:
            return False
        try:
            runtime.close()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Releasing the runtime of Brain %s raised", brain_id)
        return True

    async def shutdown(self) -> None:
        """Release every runtime held by this process."""
        async with self._allocation_lock:
            for brain_id in list(self._runtimes):
                await self._release_handle(brain_id)
                await self.capacity.release(brain_id)

    # -- reconciliation ----------------------------------------------------
    async def reconcile(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Compare the registry with the runtimes that actually exist.

        A Brain whose registry row claims a live runtime but whose runtime is
        gone (for example after a process restart) is reported as
        ``runtime_missing`` so the caller can re-provision it.
        """
        from app.brain_manager.registry import repository

        brains, _ = await repository.list_brains(session, limit=1000, offset=0)
        report: list[dict[str, Any]] = []
        for brain in brains:
            runtime = self._runtimes.get(brain.brain_id)
            registry_status = brain.llm_runtime_status
            if runtime is None:
                if registry_status in {
                    BrainRuntimeStatus.READY.value,
                    BrainRuntimeStatus.READY_SIMULATED.value,
                    BrainRuntimeStatus.READY_EXTERNAL.value,
                    BrainRuntimeStatus.DEGRADED.value,
                    BrainRuntimeStatus.STARTING.value,
                }:
                    brain.llm_runtime_status = BrainRuntimeStatus.STOPPED.value
                    brain.runtime_last_error = "runtime_missing"
                    await session.commit()
                    report.append(
                        {
                            "brain_id": str(brain.brain_id),
                            "action": "runtime_missing",
                            "registry_status": registry_status,
                            "new_status": BrainRuntimeStatus.STOPPED.value,
                        }
                    )
                continue

            health = runtime.health()
            if not health.healthy and registry_status in {
                BrainRuntimeStatus.READY.value,
                BrainRuntimeStatus.READY_SIMULATED.value,
                BrainRuntimeStatus.READY_EXTERNAL.value,
            }:
                brain.llm_runtime_status = BrainRuntimeStatus.DEGRADED.value
                brain.runtime_last_error = health.detail or health.status
                await session.commit()
                report.append(
                    {
                        "brain_id": str(brain.brain_id),
                        "action": "runtime_degraded",
                        "registry_status": registry_status,
                        "new_status": BrainRuntimeStatus.DEGRADED.value,
                    }
                )
        return report


#: The process-wide runtime manager. The API layer, the provisioning
#: orchestrator and the inference path all use this instance, so a runtime
#: created for a Brain is the same object that serves its requests.
_runtime_manager: BrainRuntimeManager | None = None


def get_runtime_manager() -> BrainRuntimeManager:
    """Return the process-wide runtime manager."""
    global _runtime_manager
    if _runtime_manager is None:
        _runtime_manager = BrainRuntimeManager()
    return _runtime_manager


def reset_runtime_manager() -> None:
    """Drop the cached runtime manager (used by the test-suite)."""
    global _runtime_manager
    _runtime_manager = None
