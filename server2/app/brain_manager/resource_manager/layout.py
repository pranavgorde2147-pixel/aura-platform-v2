"""The logical resource layout of one Brain.

The tree below is the *logical* organisation of a Brain's resources. It is a
manifest, not a promise that a directory with those names exists on disk: the
actual bytes live in the metadata database, the object store and the vector
index. Where a physical directory is genuinely useful (runtime scratch space,
manifest files, job artefacts) it is created through the trusted path helpers
in :mod:`app.brain_manager.resource_manager.paths`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Shared infrastructure that is *not* owned by any single Brain.
SHARED_INFRASTRUCTURE_TREE: tuple[str, ...] = (
    "shared_infrastructure/runtime_orchestrator",
    "shared_infrastructure/model_artifact_manager",
    "shared_infrastructure/compute_capacity_manager",
    "shared_infrastructure/system_monitoring",
)


@dataclass(frozen=True)
class ResourceNode:
    """One node of the logical Brain resource tree."""

    path: str
    kind: str  # "manifest" | "config" | "runtime" | "index" | "collection" | "directory"
    backend: str  # "database" | "object_store" | "vector_index" | "filesystem" | "process"
    description: str
    children: tuple["ResourceNode", ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "backend": self.backend,
            "description": self.description,
            "children": [child.to_dict() for child in self.children],
        }

    def flatten(self) -> list["ResourceNode"]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.flatten())
        return nodes


def _node(
    path: str,
    kind: str,
    backend: str,
    description: str,
    children: tuple[ResourceNode, ...] = (),
) -> ResourceNode:
    return ResourceNode(
        path=path, kind=kind, backend=backend, description=description, children=children
    )


#: The complete logical tree of a Brain.
BRAIN_RESOURCE_TREE: ResourceNode = _node(
    "brain",
    "directory",
    "logical",
    "Root of one Brain's managed resources.",
    children=(
        _node("brain_manifest.json", "manifest", "filesystem", "Resource manifest of this Brain."),
        _node(
            "identity",
            "directory",
            "logical",
            "Brain identity and owner association (canonical ids live in the registry).",
        ),
        _node(
            "personal_llm",
            "directory",
            "logical",
            "The Brain's own language-model resources.",
            children=(
                _node("personal_llm/model_config.json", "config", "filesystem", "Model configuration."),
                _node(
                    "personal_llm/runtime_metadata.json",
                    "config",
                    "filesystem",
                    "Runtime identity, isolation level and health.",
                ),
                _node(
                    "personal_llm/inference_runtime",
                    "runtime",
                    "process",
                    "The dedicated inference runtime owned by this Brain.",
                ),
                _node(
                    "personal_llm/context_manager",
                    "collection",
                    "database",
                    "Per-Brain inference context and conversation history.",
                ),
                _node(
                    "personal_llm/runtime_cache",
                    "collection",
                    "database",
                    "Per-Brain runtime cache (never shared between Brains).",
                ),
            ),
        ),
        _node(
            "brain_compute",
            "directory",
            "logical",
            "Compute allocation and job scheduling for this Brain.",
            children=(
                _node("brain_compute/compute_config.json", "config", "filesystem", "Compute allocation."),
                _node(
                    "brain_compute/job_queue",
                    "collection",
                    "database",
                    "Queued processing jobs owned by this Brain.",
                ),
                _node(
                    "brain_compute/task_scheduler",
                    "collection",
                    "database",
                    "Task scheduling state owned by this Brain.",
                ),
                _node(
                    "brain_compute/resource_accounting",
                    "collection",
                    "database",
                    "Usage and resource accounting for this Brain.",
                ),
                _node(
                    "brain_compute/job_status",
                    "collection",
                    "database",
                    "Job lifecycle status for this Brain.",
                ),
            ),
        ),
        _node(
            "personal_memory",
            "directory",
            "logical",
            "This Brain's personal memory subsystem.",
            children=(
                _node("personal_memory/short_term", "collection", "database", "Short-term memory."),
                _node("personal_memory/long_term", "collection", "database", "Long-term memory."),
                _node("personal_memory/episodic", "collection", "database", "Episodic memory."),
                _node("personal_memory/semantic", "collection", "database", "Semantic memory."),
                _node("personal_memory/memory_index", "index", "vector_index", "Memory index."),
            ),
        ),
        _node(
            "vector_engine",
            "directory",
            "logical",
            "This Brain's own vector index and retrieval configuration.",
            children=(
                _node("vector_engine/embeddings", "collection", "database", "Embedding chunks."),
                _node("vector_engine/vector_index", "index", "vector_index", "Private vector space."),
                _node("vector_engine/retrieval_config.json", "config", "filesystem", "Retrieval config."),
            ),
        ),
        _node(
            "personal_vault",
            "directory",
            "logical",
            "This Brain's isolated personal storage vault.",
            children=(
                _node("personal_vault/records", "collection", "database", "Vault records."),
                _node("personal_vault/documents", "collection", "object_store", "Vault documents."),
                _node("personal_vault/audio", "collection", "object_store", "Vault audio objects."),
                _node("personal_vault/images", "collection", "object_store", "Vault image objects."),
                _node(
                    "personal_vault/structured_data",
                    "collection",
                    "database",
                    "Structured vault data.",
                ),
                _node("personal_vault/metadata", "collection", "database", "Vault metadata."),
                _node("personal_vault/relationships", "collection", "database", "Vault relationships."),
            ),
        ),
        _node(
            "connected_apps",
            "collection",
            "database",
            "This Brain's authorized app connections and consent state.",
        ),
        _node(
            "brain_processing",
            "directory",
            "logical",
            "This Brain's processing pipeline.",
            children=(
                _node("brain_processing/classification", "collection", "database", "Classification output."),
                _node("brain_processing/data_sorting", "collection", "database", "Sorting and organization."),
                _node("brain_processing/summarization", "collection", "database", "Summaries."),
                _node("brain_processing/entity_extraction", "collection", "database", "Extracted entities."),
                _node(
                    "brain_processing/relationship_detection",
                    "collection",
                    "database",
                    "Detected relationships.",
                ),
                _node(
                    "brain_processing/embedding_generation",
                    "collection",
                    "database",
                    "Generated embeddings.",
                ),
                _node("brain_processing/retrieval", "collection", "database", "Retrieval results."),
                _node(
                    "brain_processing/context_construction",
                    "collection",
                    "database",
                    "Constructed inference contexts.",
                ),
            ),
        ),
        _node("conversations", "collection", "database", "This Brain's conversations."),
        _node("configuration", "collection", "filesystem", "This Brain's configuration."),
        _node("integrations", "collection", "database", "This Brain's integrations."),
        _node("usage", "collection", "database", "This Brain's usage records."),
        _node("audit", "collection", "database", "This Brain's audit trail."),
        _node("logs", "collection", "filesystem", "This Brain's runtime logs."),
    ),
)


def build_resource_tree() -> dict[str, Any]:
    """Return the logical resource tree as a serialisable structure."""
    return {
        "brain": BRAIN_RESOURCE_TREE.to_dict(),
        "shared_infrastructure": [
            {"path": path, "kind": "shared", "backend": "mixed"}
            for path in SHARED_INFRASTRUCTURE_TREE
        ],
    }
