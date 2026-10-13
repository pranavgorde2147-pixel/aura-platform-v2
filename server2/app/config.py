"""Backward-compatible configuration shim.

The single source of truth is :mod:`app.core.config`. This module re-exports
its settings so older imports keep working without maintaining a second,
conflicting configuration surface.
"""

from app.core.config import Config, Settings, settings  # noqa: F401

__all__ = ["Config", "Settings", "settings"]
