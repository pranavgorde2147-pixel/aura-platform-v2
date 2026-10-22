"""Status vocabularies used by the Brain registry.

The values are intentionally explicit. In particular there is no "shared"
shorthand for the runtime status: a Brain either owns a dedicated runtime, owns
a context-isolated handle on shared weights, or owns a simulated runtime, and
the registry says which one it is.
"""

from __future__ import annotations

from enum import Enum


class BrainProvisioningStatus(str, Enum):
    """Lifecycle of the Brain as a whole."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    RECONCILING = "reconciling"
    DECOMMISSIONED = "decommissioned"


class BrainRuntimeStatus(str, Enum):
    """Lifecycle of the Brain's dedicated LLM runtime."""

    UNREGISTERED = "unregistered"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    READY = "ready"
    #: A dedicated runtime object exists but is backed by the deterministic
    #: simulator. Never presented as real inference.
    READY_SIMULATED = "ready_simulated"
    #: A dedicated handle exists but the underlying weights/provider are shared.
    READY_EXTERNAL = "ready_external"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"
    WAITING_FOR_RESOURCES = "waiting_for_resources"
    RESOURCE_LIMITED = "resource_limited"


class ResourceAvailability(str, Enum):
    """Outcome of a compute-capacity reservation attempt."""

    AVAILABLE = "available"
    WAITING = "waiting_for_resources"
    LIMITED = "resource_limited"


#: Statuses in which a Brain may serve inference.
SERVING_RUNTIME_STATUSES = frozenset(
    {
        BrainRuntimeStatus.READY,
        BrainRuntimeStatus.READY_SIMULATED,
        BrainRuntimeStatus.READY_EXTERNAL,
    }
)

#: Runtime statuses that mean "no runtime is currently usable".
NON_SERVING_RUNTIME_STATUSES = frozenset(
    {
        BrainRuntimeStatus.UNREGISTERED,
        BrainRuntimeStatus.PROVISIONING,
        BrainRuntimeStatus.STARTING,
        BrainRuntimeStatus.FAILED,
        BrainRuntimeStatus.STOPPED,
        BrainRuntimeStatus.WAITING_FOR_RESOURCES,
        BrainRuntimeStatus.RESOURCE_LIMITED,
    }
)
