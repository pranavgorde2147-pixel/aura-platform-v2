"""Configuration for Server 2 object storage.

All values are environment driven. No credentials are ever hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the storage engine."""

    service_name: str = "AURA Storage Engine"
    version: str = "1.0.0"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8001

    # Internal service authentication. Required for all object endpoints.
    internal_api_token: str = ""

    # Storage backend: "local" (filesystem) or "s3" (MinIO / S3-compatible).
    backend: str = "local"
    data_dir: str = "./data"
    local_root: str = "./data/objects"
    vault_backup_root: str = ""

    # S3 / MinIO configuration (used when backend == "s3").
    s3_endpoint_url: str = ""
    s3_bucket: str = "aura-objects"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False

    # Metadata database. Defaults to an embedded SQLite database so the
    # service is self-contained. Point at PostgreSQL in production.
    database_url: str = ""

    # Limits and policy.
    max_upload_size_bytes: int = 50 * 1024 * 1024
    allowed_content_types: tuple[str, ...] = field(default_factory=tuple)
    default_retention_days: int = 0  # 0 == no automatic expiry

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{Path(self.data_dir) / 'storage.db'}"

    @property
    def resolved_local_root(self) -> Path:
        return Path(self.local_root)

    @property
    def resolved_vault_backup_root(self) -> Path:
        server_root = Path(__file__).resolve().parents[1]
        configured = (
            Path(self.vault_backup_root).expanduser()
            if self.vault_backup_root
            else Path("brain_vault_backups")
        )
        return configured if configured.is_absolute() else server_root / configured


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings read from the environment."""
    raw_allowed = os.getenv("ALLOWED_CONTENT_TYPES", "").strip()
    allowed = tuple(t.strip() for t in raw_allowed.split(",") if t.strip())

    settings = Settings(
        service_name=os.getenv("SERVICE_NAME", "AURA Storage Engine"),
        version=os.getenv("SERVICE_VERSION", "1.0.0"),
        environment=os.getenv("ENVIRONMENT", "development"),
        host=os.getenv("HOST", "0.0.0.0"),
        port=_int("PORT", 8001),
        internal_api_token=os.getenv("STORAGE_API_TOKEN", "").strip()
        or os.getenv("INTERNAL_API_TOKEN", "").strip(),
        backend=os.getenv("STORAGE_BACKEND", "local").strip().lower(),
        data_dir=os.getenv("STORAGE_DATA_DIR", "./data"),
        local_root=os.getenv("STORAGE_LOCAL_ROOT", os.getenv("STORAGE_DATA_DIR", "./data") + "/objects"),
        vault_backup_root=os.getenv("BRAIN_VAULT_BACKUP_ROOT", "").strip(),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", "").strip(),
        s3_bucket=os.getenv("S3_BUCKET", "aura-objects").strip(),
        s3_access_key=os.getenv("S3_ACCESS_KEY", "").strip(),
        s3_secret_key=os.getenv("S3_SECRET_KEY", "").strip(),
        s3_region=os.getenv("S3_REGION", "us-east-1").strip(),
        s3_use_ssl=_bool("S3_USE_SSL", False),
        database_url=os.getenv("STORAGE_DATABASE_URL", "").strip(),
        max_upload_size_bytes=_int("MAX_UPLOAD_SIZE_BYTES", 50 * 1024 * 1024),
        allowed_content_types=allowed,
        default_retention_days=_int("DEFAULT_RETENTION_DAYS", 0),
    )
    return settings
