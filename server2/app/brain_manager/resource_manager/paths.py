"""Trusted server-side path construction for Brain resource trees.

Client input never becomes a filesystem path. Every path is built from a
server-owned root plus server-owned components (a UUID, a fixed directory
name). ``safe_join`` is the only way to extend a path and it rejects anything
that could escape the root.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from app.brain_manager.config import get_brain_manager_config

#: Characters that are never allowed in a path component.
_FORBIDDEN_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


class InvalidResourcePathError(ValueError):
    """Raised when a path component would escape its trusted root."""


def sanitize_component(value: str, *, allow_slash: bool = False) -> str:
    """Return a single safe path component.

    Raises :class:`InvalidResourcePathError` for empty values, path separators,
    parent-directory references, NUL bytes and any other unexpected character.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidResourcePathError("Resource path component must be a non-empty string")
    candidate = value.strip()
    if "\x00" in candidate:
        raise InvalidResourcePathError("Resource path component contains a NUL byte")
    if ".." in candidate:
        raise InvalidResourcePathError("Resource path component must not contain '..'")
    if not allow_slash and ("/" in candidate or "\\" in candidate):
        raise InvalidResourcePathError("Resource path component must not contain a path separator")
    if candidate in {".", ".."}:
        raise InvalidResourcePathError("Resource path component is not a valid name")
    if _FORBIDDEN_COMPONENT.search(candidate):
        raise InvalidResourcePathError(
            "Resource path component contains unsupported characters"
        )
    return candidate


def safe_join(root: Path, *parts: str) -> Path:
    """Join trusted components under ``root``, refusing to escape it.

    The final path is verified to be inside ``root`` after resolution, so even
    a component that slipped past validation cannot traverse upwards.
    """
    if not parts:
        raise InvalidResourcePathError("At least one path component is required")

    base = Path(root).resolve()
    candidate = base
    for part in parts:
        cleaned = sanitize_component(part)
        candidate = candidate / cleaned

    resolved = candidate.resolve() if candidate.exists() else Path(os.path.normpath(candidate))
    if resolved != base and base not in resolved.parents:
        raise InvalidResourcePathError("Resolved path escapes the trusted resource root")
    return resolved


def brain_resource_root() -> Path:
    """Return the configured root directory that holds every Brain tree."""
    return get_brain_manager_config().resolved_resource_root


def brain_directory_name(brain_id: uuid.UUID) -> str:
    """Return the canonical directory name of one Brain."""
    return f"brain_{uuid.UUID(str(brain_id))}"


def brain_root(brain_id: uuid.UUID, *, base: Path | None = None) -> Path:
    """Return the resource root of one Brain (created on demand by callers)."""
    root = Path(base) if base is not None else brain_resource_root()
    return safe_join(root, brain_directory_name(brain_id))


def brain_relative_path(brain_id: uuid.UUID, *parts: str) -> str:
    """Return a Brain-relative path string used inside resource references.

    The result is a logical reference (``brain_<uuid>/...``), never an absolute
    filesystem path, so it can be stored in a registry row and returned by the
    admin API without leaking the server's directory layout.
    """
    return "/".join([brain_directory_name(brain_id), *(sanitize_component(p) for p in parts)])
