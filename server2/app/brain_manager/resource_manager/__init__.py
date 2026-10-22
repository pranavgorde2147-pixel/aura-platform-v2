"""Resource management for per-Brain resource trees."""

from __future__ import annotations

from app.brain_manager.resource_manager.layout import (
    BRAIN_RESOURCE_TREE,
    build_resource_tree,
)
from app.brain_manager.resource_manager.paths import (
    InvalidResourcePathError,
    brain_resource_root,
    brain_root,
    safe_join,
    sanitize_component,
)

__all__ = [
    "BRAIN_RESOURCE_TREE",
    "InvalidResourcePathError",
    "brain_resource_root",
    "brain_root",
    "build_resource_tree",
    "safe_join",
    "sanitize_component",
]
