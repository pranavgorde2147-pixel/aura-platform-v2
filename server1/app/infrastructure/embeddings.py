"""Embedding providers for semantic memory.

The default provider is deterministic and dependency-free: it hashes word and
character n-gram tokens into a fixed-size vector and L2-normalizes the result.
It is not a neural embedding, but it is a *real* vector representation that
enables the full index/retrieve pathway to run offline and in tests without a
GPU or a network model download.

A production deployment can swap in a neural provider by implementing the
``EmbeddingProvider`` protocol and wiring it in ``get_embedding_provider``.
The vector dimension must match the Qdrant collection; changing providers
requires recreating the collection.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingProvider(Protocol):
    """Contract for turning text into fixed-dimension vectors."""

    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text."""


def _bucket(token: str, dimension: int) -> int:
    """Map a token to a stable bucket without relying on salted ``hash()``."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimension


class HashingEmbeddingProvider:
    """Deterministic, offline bag-of-n-grams embedding.

    Combines whole-word tokens with character trigrams so that related word
    forms (for example singular/plural) still share signal. The vector is
    L2-normalized, so cosine similarity behaves like a normalized term match.
    """

    def __init__(self, dimension: int = 256) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = _TOKEN_RE.findall((text or "").lower())
        for token in tokens:
            vector[_bucket(token, self.dimension)] += 1.0
            padded = f"^{token}$"
            for i in range(len(padded) - 2):
                vector[_bucket(padded[i : i + 3], self.dimension)] += 0.5
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """Return the process-wide embedding provider."""
    settings = get_settings()
    return HashingEmbeddingProvider(dimension=settings.embedding_dimension)
