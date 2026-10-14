"""Object storage backend interface and factory."""

from __future__ import annotations

from typing import BinaryIO, Iterator, Protocol


class ObjectStore(Protocol):
    """Minimal interface implemented by every storage backend."""

    name: str

    def save(self, key: str, stream: BinaryIO, size: int) -> None:
        """Persist ``size`` bytes from ``stream`` under ``key``."""

    def open_stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Yield the object's bytes in chunks."""

    def delete(self, key: str) -> None:
        """Delete the object if it exists."""

    def exists(self, key: str) -> bool:
        """Return True if the object exists."""

    def health(self) -> bool:
        """Return True if the backend is reachable/usable."""


def get_object_store() -> ObjectStore:
    """Return the configured object storage backend (cached per settings)."""
    from app.storage_config import get_settings
    from app.storage.local import LocalObjectStore
    from app.storage.s3 import S3ObjectStore

    settings = get_settings()
    if settings.backend == "s3":
        return S3ObjectStore(
            endpoint_url=settings.s3_endpoint_url,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            use_ssl=settings.s3_use_ssl,
        )
    return LocalObjectStore(root=settings.resolved_local_root)


_object_store: ObjectStore | None = None


def get_cached_object_store() -> ObjectStore:
    """Process-wide cached backend instance."""
    global _object_store
    if _object_store is None:
        _object_store = get_object_store()
    return _object_store


def reset_object_store() -> None:
    """Clear the cached backend (used by tests)."""
    global _object_store
    _object_store = None
