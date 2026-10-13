"""Centralized infrastructure exception types."""

from __future__ import annotations


class InfrastructureError(Exception):
    """Base exception for infrastructure-related failures."""


class DatabaseConnectionError(InfrastructureError):
    """Raised when a database connection cannot be established."""


class RedisConnectionError(InfrastructureError):
    """Raised when a Redis connection cannot be established."""


class QdrantConnectionError(InfrastructureError):
    """Raised when a Qdrant connection cannot be established."""
