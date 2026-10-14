"""Local filesystem object storage backend."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO, Iterator

_SAFE_PARTS = ("..", "", ".")


def _safe_key(key: str) -> str:
    """Reject keys that could escape the storage root."""
    normalised = key.replace("\\", "/").lstrip("/")
    parts = normalised.split("/")
    if any(part in _SAFE_PARTS for part in parts):
        raise ValueError("Unsafe object key")
    return normalised


class LocalObjectStore:
    """Stores objects on the local filesystem rooted at ``root``."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = _safe_key(key)
        target = (self.root / safe).resolve()
        root = self.root.resolve()
        if root not in target.parents and target != root:
            raise ValueError("Object key escapes storage root")
        return target

    def save(self, key: str, stream: BinaryIO, size: int) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".part")
        written = 0
        with open(tmp_path, "wb") as handle:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > size:
                    raise ValueError("Uploaded data exceeds declared size")
                handle.write(chunk)
        if written != size:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("Uploaded data size does not match declared size")
        os.replace(tmp_path, path)

    def open_stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        path = self._path(key)
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
        parent = path.parent
        root = self.root.resolve()
        if parent != root:
            try:
                shutil.rmtree(parent)
            except OSError:
                pass

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except ValueError:
            return False

    def health(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except Exception:
            return False
