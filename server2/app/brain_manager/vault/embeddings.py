"""Deterministic embedding provider for the Brain vector index.

Server 2 has no neural embedding model of its own, so the vector index uses a
hashing bag-of-n-grams embedding: deterministic, dependency-free and stable
across processes. It is a *real* vector representation (L2-normalised, cosine
comparable), which is what the retrieval path needs; it is not a neural
embedding and the registry says so via ``model_id="deterministic-v1"``.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[a-z0-9]+")

DEFAULT_DIMENSION = 256
MODEL_ID = "deterministic-v1"


def _bucket(token: str, dimension: int) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimension


class HashingEmbeddingProvider:
    """Deterministic bag-of-n-grams embedding."""

    model_id = MODEL_ID

    def __init__(self, dimension: int = DEFAULT_DIMENSION) -> None:
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
            for index in range(len(padded) - 2):
                vector[_bucket(padded[index : index + 3], self.dimension)] += 0.5
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Return the cosine similarity of two equally-sized vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


@lru_cache(maxsize=1)
def get_embedding_provider() -> HashingEmbeddingProvider:
    """Return the process-wide embedding provider."""
    return HashingEmbeddingProvider()
