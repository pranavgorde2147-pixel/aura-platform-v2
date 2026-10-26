"""Physically isolated SQLite vaults and object roots for Brain IDs."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.brain_manager.db_models import VaultBase
from app.brain_manager.resource_manager.paths import (
    brain_directory_name,
    brain_resource_root,
    sanitize_component,
)
from app.storage_config import get_settings
from app.storage_db import ManagedBrain


class BrainVaultStorageError(RuntimeError):
    """Raised when a Brain's private storage cannot be safely opened."""


class BrainVaultStorageManager:
    """Creates and opens the private files belonging to a trusted registry Brain."""

    def __init__(self) -> None:
        self._engines: dict[uuid.UUID, AsyncEngine] = {}
        self._factories: dict[uuid.UUID, async_sessionmaker[AsyncSession]] = {}
        self._vault_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        """Return the canonical root containing the registered Brain folders."""
        configured = brain_resource_root().expanduser()
        configured.mkdir(parents=True, exist_ok=True, mode=0o700)
        canonical = configured.resolve(strict=True)
        if not canonical.is_dir():
            raise BrainVaultStorageError("Configured Brain vault root is not a directory")
        try:
            canonical.chmod(0o700)
        except OSError as exc:
            raise BrainVaultStorageError("Could not restrict Brain vault root permissions") from exc
        return canonical

    def brain_directory(self, brain: ManagedBrain) -> Path:
        """Resolve a Brain directory from the trusted registry ID, never request paths."""
        brain_id = uuid.UUID(str(brain.brain_id))
        root = brain_resource_root().expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root = root.resolve(strict=True)
            root.chmod(0o700)
            resource_name = sanitize_component(
                getattr(brain, "resource_root", None) or brain_directory_name(brain_id)
            )
            directory = root / resource_name
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
        except OSError as exc:
            raise BrainVaultStorageError("Could not create Brain's private storage directory") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BrainVaultStorageError("Brain storage directory must be a real directory")
        if directory.resolve(strict=True).parent != root:
            raise BrainVaultStorageError("Brain storage directory escapes the configured root")
        if not directory.name.endswith(str(brain_id)):
            raise BrainVaultStorageError("Brain storage directory does not match its Brain ID")
        directory.chmod(0o700)
        return directory

    def memory_vault_directory(self, brain: ManagedBrain) -> Path:
        path = self.brain_directory(brain) / "memory_vault"
        path.mkdir(mode=0o700, exist_ok=True)
        self._verify_private_child(brain, path)
        return path

    def object_directory(self, brain: ManagedBrain) -> Path:
        path = self.brain_directory(brain) / "objects"
        path.mkdir(mode=0o700, exist_ok=True)
        self._verify_private_child(brain, path)
        return path

    def index_directory(self, brain: ManagedBrain) -> Path:
        path = self.brain_directory(brain) / "indexes"
        path.mkdir(mode=0o700, exist_ok=True)
        self._verify_private_child(brain, path)
        return path

    def database_path(self, brain: ManagedBrain) -> Path:
        return self.memory_vault_directory(brain) / "vault.sqlite"

    def _backup_root(self, brain: ManagedBrain) -> Path:
        root = get_settings().resolved_vault_backup_root.expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = root.resolve(strict=True)
        root.chmod(0o700)
        brain_root = root / f"brain_{uuid.UUID(str(brain.brain_id))}"
        brain_root.mkdir(mode=0o700, exist_ok=True)
        if brain_root.is_symlink() or brain_root.resolve(strict=True).parent != root:
            raise BrainVaultStorageError("Brain backup path escapes the configured backup root")
        brain_root.chmod(0o700)
        return brain_root

    async def backup(self, brain: ManagedBrain, backup_id: uuid.UUID | None = None) -> Path:
        """Create a consistent, independent backup of one vault and its objects."""
        await self.ensure_vault(brain)
        identifier = backup_id or uuid.uuid4()
        backup_root = self._backup_root(brain)
        destination = backup_root / str(identifier)
        if destination.exists():
            raise FileExistsError("A backup with this identifier already exists")
        staging = Path(
            tempfile.mkdtemp(prefix=f".{identifier}.", dir=backup_root)
        )
        staging.chmod(0o700)
        try:
            source = sqlite3.connect(self.database_path(brain))
            backup_database = staging / "memory_vault" / "vault.sqlite"
            backup_database.parent.mkdir(mode=0o700)
            target = sqlite3.connect(backup_database)
            try:
                source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise BrainVaultStorageError("Vault backup failed SQLite integrity_check")
            finally:
                target.close()
                source.close()
            for folder in ("objects", "indexes"):
                source_dir = self.brain_directory(brain) / folder
                self._copy_private_tree(source_dir, staging / folder)
            backup_database.chmod(0o600)
            os.replace(staging, destination)
            return destination
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    async def restore(self, brain: ManagedBrain, backup_id: uuid.UUID) -> None:
        """Restore one Brain from a verified backup, keeping rollback data on failure."""
        source = self._backup_root(brain) / str(uuid.UUID(str(backup_id)))
        self._verify_backup_directory(brain, source)
        database = source / "memory_vault" / "vault.sqlite"
        if database.is_symlink() or not database.is_file():
            raise BrainVaultStorageError("Backup is missing its vault database")
        with sqlite3.connect(database) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BrainVaultStorageError("Backup failed SQLite integrity_check")

        destination = self.brain_directory(brain)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
        staging.chmod(0o700)
        previous = Path(tempfile.mkdtemp(prefix=".restore-previous-", dir=destination.parent))
        previous.chmod(0o700)
        replaced: list[tuple[str, bool]] = []
        try:
            for folder in ("memory_vault", "objects", "indexes"):
                self._copy_private_tree(source / folder, staging / folder)
            await self._dispose_brain_engine(uuid.UUID(str(brain.brain_id)))
            for folder in ("memory_vault", "objects", "indexes"):
                target = destination / folder
                had_previous = target.exists()
                if had_previous:
                    os.replace(target, previous / folder)
                replaced.append((folder, had_previous))
                os.replace(staging / folder, target)
            shutil.rmtree(previous)
        except Exception:
            for folder, had_previous in reversed(replaced):
                target = destination / folder
                if target.exists():
                    shutil.rmtree(target)
                old = previous / folder
                if had_previous and old.exists():
                    os.replace(old, target)
            if staging.exists():
                shutil.rmtree(staging)
            if previous.exists():
                shutil.rmtree(previous)
            raise
        if staging.exists():
            shutil.rmtree(staging)

    async def delete(self, brain: ManagedBrain, *, owner_id: uuid.UUID, confirm: bool) -> None:
        """Delete only the registry-matched Brain vault after explicit confirmation."""
        if not confirm or uuid.UUID(str(owner_id)) != uuid.UUID(str(brain.owner_id)):
            raise PermissionError("Vault deletion requires the matching owner and confirmation")
        brain_id = uuid.UUID(str(brain.brain_id))
        await self._dispose_brain_engine(brain_id)
        directory = self.brain_directory(brain)
        for folder in ("memory_vault", "objects", "indexes"):
            path = directory / folder
            if not path.exists():
                continue
            self._verify_private_child(brain, path)
            shutil.rmtree(path)

    def _verify_backup_directory(self, brain: ManagedBrain, source: Path) -> None:
        root = self._backup_root(brain)
        if source.is_symlink() or not source.is_dir() or source.resolve(strict=True).parent != root:
            raise BrainVaultStorageError("Backup path is invalid")

    @staticmethod
    def _copy_private_tree(source: Path, destination: Path) -> None:
        if source.is_symlink() or not source.is_dir():
            raise BrainVaultStorageError("Vault backup contains an invalid directory")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise BrainVaultStorageError("Vault backup refuses symbolic links")
        shutil.copytree(source, destination, dirs_exist_ok=True)
        destination.chmod(0o700)
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
            elif path.is_dir():
                path.chmod(0o700)

    async def _dispose_brain_engine(self, brain_id: uuid.UUID) -> None:
        async with self._lock:
            engine = self._engines.pop(brain_id, None)
            self._factories.pop(brain_id, None)
        if engine is not None:
            await engine.dispose()

    def _verify_private_child(self, brain: ManagedBrain, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise BrainVaultStorageError("Brain storage path must be a real directory")
        if path.resolve(strict=True).parent != self.brain_directory(brain):
            raise BrainVaultStorageError("Brain storage path escapes its private directory")
        path.chmod(0o700)

    async def ensure_vault(self, brain: ManagedBrain) -> None:
        """Initialize one vault idempotently, including its private folders and schema."""
        brain_id = uuid.UUID(str(brain.brain_id))
        async with self._lock:
            vault_lock = self._vault_locks.setdefault(brain_id, asyncio.Lock())
        async with vault_lock:
            await self._ensure_vault_locked(brain)

    async def _ensure_vault_locked(self, brain: ManagedBrain) -> None:
        """Perform vault initialization while holding its per-Brain lock."""
        brain_dir = self.brain_directory(brain)
        self.object_directory(brain)
        self.index_directory(brain)
        vault_directory = self.memory_vault_directory(brain)
        database = vault_directory / "vault.sqlite"
        if database.is_symlink():
            raise BrainVaultStorageError("Vault database must not be a symbolic link")
        try:
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not database.is_file():
                raise BrainVaultStorageError("Vault database path is not a regular file")
        except OSError as exc:
            raise BrainVaultStorageError("Could not create Brain's private vault database") from exc
        else:
            os.close(descriptor)
        database.chmod(0o600)

        engine = await self._get_engine(brain)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            await connection.run_sync(
                lambda sync_connection: VaultBase.metadata.create_all(sync_connection)
            )
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")

    async def _get_engine(self, brain: ManagedBrain) -> AsyncEngine:
        brain_id = uuid.UUID(str(brain.brain_id))
        async with self._lock:
            engine = self._engines.get(brain_id)
            if engine is not None:
                return engine
            path = self.database_path(brain)
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{path}",
                connect_args={"timeout": 30},
                pool_pre_ping=True,
            )
            self._engines[brain_id] = engine
            self._factories[brain_id] = async_sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False
            )
            return engine

    @asynccontextmanager
    async def session(self, brain: ManagedBrain) -> AsyncIterator[AsyncSession]:
        await self.ensure_vault(brain)
        brain_id = uuid.UUID(str(brain.brain_id))
        factory = self._factories[brain_id]
        async with factory() as session:
            yield session

    async def dispose(self) -> None:
        """Close all cached engines, e.g. when Server 2 shuts down."""
        async with self._lock:
            engines = tuple(self._engines.values())
            self._engines.clear()
            self._factories.clear()
            self._vault_locks.clear()
        for engine in engines:
            await engine.dispose()

    def clear_for_tests(self) -> None:
        """Forget cached engines after a test has disposed the previous event loop."""
        self._engines.clear()
        self._factories.clear()


_manager = BrainVaultStorageManager()


def get_brain_vault_storage() -> BrainVaultStorageManager:
    return _manager


async def dispose_brain_vault_storage() -> None:
    await _manager.dispose()


__all__ = [
    "BrainVaultStorageError",
    "BrainVaultStorageManager",
    "dispose_brain_vault_storage",
    "get_brain_vault_storage",
]
