"""Resolution, private model provisioning, and validation of model artifacts."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from app.brain_manager.runtime.base import RuntimeProvisioningError
from app.brain_manager.resource_manager.paths import (
    brain_root,
    brain_directory_name,
    brain_resource_root,
    sanitize_component,
)

_copy_locks: dict[uuid.UUID, asyncio.Lock] = {}


def shared_model_artifact(source_path: Path) -> Path:
    """Return the canonical shared model path without copying model weights."""
    source = Path(source_path)
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise RuntimeProvisioningError(
            "model_artifact_missing",
            f"Model artifact is not available at {Path(source_path).name}",
        ) from exc

    if not source.is_file() and not source.is_dir():
        raise RuntimeProvisioningError(
            "model_artifact_invalid",
            "Configured model artifact is not a file or directory",
        )
    return source


def model_artifact_for_brain(
    source_path: Path,
    *,
    brain_id: uuid.UUID,
    resource_root: str | None,
) -> tuple[Path, bool]:
    """Resolve the private model copy provisioned for this Brain."""
    source = Path(source_path)
    root = brain_resource_root().expanduser()
    relative_root = sanitize_component(
        resource_root or brain_directory_name(brain_id)
    )
    brain_directory = root / relative_root
    model_weights = brain_directory / "personal_llm" / "model_weights"
    if source.is_dir():
        local_candidate = model_weights
        private_model_is_complete = (local_candidate / "config.json").is_file()
    else:
        local_candidate = model_weights / source.name
        private_model_is_complete = local_candidate.is_file()
    if private_model_is_complete:
        return shared_model_artifact(local_candidate), True
    if resource_root:
        raise RuntimeProvisioningError(
            "private_model_missing",
            "Brain's private model artifact has not been provisioned",
        )
    return shared_model_artifact(source), False


def _private_model_path(
    source_path: Path,
    *,
    brain_id: uuid.UUID,
    resource_root: str | None,
) -> tuple[Path, Path]:
    source = shared_model_artifact(source_path)
    root = brain_root(brain_id, resource_root=resource_root)
    weights_root = root / "personal_llm" / "model_weights"
    weights_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if weights_root.parent.is_symlink() or weights_root.parent.resolve().parent != root.resolve():
        raise RuntimeProvisioningError(
            "private_model_path_invalid", "Brain model directory escapes its resource root"
        )
    return source, weights_root


def _private_model_is_complete(source: Path, destination: Path) -> bool:
    if source.is_dir():
        return destination.is_dir() and not destination.is_symlink() and (
            destination / "config.json"
        ).is_file()
    return destination.is_file() and not destination.is_symlink()


def _copy_model_artifact(source: Path, weights_root: Path) -> Path:
    destination = weights_root
    if _private_model_is_complete(source, destination if source.is_dir() else destination / source.name):
        return destination if source.is_dir() else destination / source.name
    if destination.exists():
        raise RuntimeProvisioningError(
            "private_model_incomplete",
            "Brain already has an incomplete private model directory; refusing to overwrite it",
        )

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-{uuid.uuid4().hex}.", dir=destination.parent)
    )
    stage.chmod(0o700)
    try:
        if source.is_dir():
            if any(path.is_symlink() for path in source.rglob("*")):
                raise RuntimeProvisioningError(
                    "model_artifact_invalid", "Model directory contains symbolic links"
                )
            shutil.copytree(source, stage, dirs_exist_ok=True)
            for path in stage.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
                elif path.is_file():
                    path.chmod(0o600)
        else:
            target = stage / source.name
            shutil.copy2(source, target)
            target.chmod(0o600)
        os.replace(stage, destination)
    except FileExistsError:
        if _private_model_is_complete(
            source, destination if source.is_dir() else destination / source.name
        ):
            shutil.rmtree(stage, ignore_errors=True)
            return destination if source.is_dir() else destination / source.name
        raise RuntimeProvisioningError(
            "private_model_copy_failed", "Could not atomically provision Brain model weights"
        )
    except RuntimeProvisioningError:
        raise
    except OSError as exc:
        raise RuntimeProvisioningError(
            "private_model_copy_failed",
            f"Could not copy model artifact into Brain storage: {type(exc).__name__}",
        ) from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return destination if source.is_dir() else destination / source.name


async def provision_private_model_artifact(
    source_path: Path,
    *,
    brain_id: uuid.UUID,
    resource_root: str | None,
) -> Path:
    """Copy a complete local model into one Brain's folder, once, atomically."""
    source, destination = _private_model_path(
        source_path, brain_id=brain_id, resource_root=resource_root
    )
    lock = _copy_locks.setdefault(brain_id, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(_copy_model_artifact, source, destination)


__all__ = [
    "model_artifact_for_brain",
    "provision_private_model_artifact",
    "shared_model_artifact",
]
