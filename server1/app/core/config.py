"""Application configuration using pydantic-settings."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object for the AURA backend foundation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="AURA Backend", alias="APP_NAME")
    app_version: str = Field(default="1.0.0", alias="APP_VERSION")
    debug: bool = Field(default=False, alias="DEBUG")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"],
        alias="CORS_ALLOWED_ORIGINS",
    )

    postgres_url: str = Field(
        default="postgresql+psycopg://aura:aura@localhost:5432/aura",
        alias="POSTGRES_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    jwt_secret_key: str = Field(default="change-me-in-production", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=15, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")
    ai_server_url: str = Field(default="http://localhost:8001", alias="AI_SERVER_URL")
    ai_server_model: str = Field(default="Qwen3.5-0.8B", alias="AI_SERVER_MODEL")
    internal_api_token: str = Field(default="", alias="INTERNAL_API_TOKEN")
    # Optional edge/gateway credentials (for example a Cloudflare Access service
    # token) sent to the Server 2 endpoint so a public tunnel only serves
    # authenticated service traffic. Both must be set to take effect.
    ai_access_client_id: str = Field(default="", alias="AI_ACCESS_CLIENT_ID")
    ai_access_client_secret: str = Field(default="", alias="AI_ACCESS_CLIENT_SECRET")
    # Object storage API (formerly Server 3, now hosted by Server 2). Falls back
    # to INTERNAL_API_TOKEN when unset.
    storage_server_url: str = Field(default="http://localhost:8001", alias="STORAGE_SERVER_URL")
    storage_api_token: str = Field(default="", alias="STORAGE_API_TOKEN")
    # Optional external AI failover used when Server 2 is unreachable
    # (for example when the local-GPU host is asleep or offline).
    ai_fallback_enabled: bool = Field(default=False, alias="AI_FALLBACK_ENABLED")
    ai_fallback_url: str = Field(default="", alias="AI_FALLBACK_URL")
    ai_fallback_key: str = Field(default="", alias="AI_FALLBACK_KEY")
    ai_fallback_model: str = Field(default="llama-3.1-8b-instant", alias="AI_FALLBACK_MODEL")
    # Outbound integration behaviour. Network failure handling is intentionally
    # bounded: a fixed number of retries with linear backoff, never a loop.
    ai_request_timeout_seconds: float = Field(default=120.0, alias="AI_REQUEST_TIMEOUT_SECONDS")
    ai_connect_timeout_seconds: float = Field(default=5.0, alias="AI_CONNECT_TIMEOUT_SECONDS")
    ai_max_retries: int = Field(default=2, alias="AI_MAX_RETRIES")
    ai_retry_backoff_seconds: float = Field(default=0.5, alias="AI_RETRY_BACKOFF_SECONDS")
    ai_verify_tls: bool = Field(default=True, alias="AI_VERIFY_TLS")
    storage_request_timeout_seconds: float = Field(default=60.0, alias="STORAGE_REQUEST_TIMEOUT_SECONDS")
    max_upload_size_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_UPLOAD_SIZE_BYTES")
    # Optional embedded Qdrant mode for local development/tests. When set, the
    # backend uses an on-disk Qdrant instance instead of a remote QDRANT_URL.
    qdrant_local_path: str = Field(default="", alias="QDRANT_LOCAL_PATH")
    # Optional embedded Redis (fakeredis) for local development/tests.
    use_fake_redis: bool = Field(default=False, alias="USE_FAKE_REDIS")

    @property
    def effective_storage_token(self) -> str:
        """Return the token used to authenticate against the storage API."""
        return self.storage_api_token or self.internal_api_token

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any, info: ValidationInfo) -> list[str]:
        """Normalize CORS origins from comma-separated or JSON array values."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in text.split(",") if item.strip()]
        return [str(value)]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for the application."""
    return Settings()
