"""Per-user Brain management for Server 2 (AURA Brain).

This package owns everything that makes a Brain a *separate* Brain:

* ``registry``        - the authoritative Brain registry (ownership + status).
* ``provisioning``    - idempotent provisioning pipeline and resource manifests.
* ``lifecycle``       - dedicated LLM runtime lifecycle per Brain.
* ``compute_scheduler`` - capacity accounting and reservations.
* ``resource_manager`` - the logical per-Brain resource layout and trusted paths.
* ``monitoring``      - health checks and registry/runtime reconciliation.

The logical resource layout maps onto the following packages::

    brain_manager/registry              -> registered_brains/brain_<uuid>/identity
    brain_manager/runtime               -> registered_brains/brain_<uuid>/personal_llm/inference_runtime
    brain_manager/provisioning/manifest -> registered_brains/brain_<uuid>/brain_manifest.json
    brain_manager/jobs                  -> registered_brains/brain_<uuid>/brain_compute/job_queue
    brain_manager/vault                 -> registered_brains/brain_<uuid>/personal_vault
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
