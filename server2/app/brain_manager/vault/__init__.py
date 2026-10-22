"""The per-Brain personal vault (records, documents, metadata, embeddings)."""

from __future__ import annotations

from app.brain_manager.vault.apps import (
    BrainAppAuthorizationError,
    BrainAppConnectionService,
)
from app.brain_manager.vault.embeddings import (
    HashingEmbeddingProvider,
    get_embedding_provider,
)
from app.brain_manager.vault.service import (
    BrainVaultService,
    CrossBrainAccessError,
    VaultRecordNotFound,
)

__all__ = [
    "BrainAppAuthorizationError",
    "BrainAppConnectionService",
    "BrainVaultService",
    "CrossBrainAccessError",
    "HashingEmbeddingProvider",
    "VaultRecordNotFound",
    "get_embedding_provider",
]
