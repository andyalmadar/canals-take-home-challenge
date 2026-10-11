from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden by an environment variable."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # SQLAlchemy URL. On Cloud Run with the Cloud SQL connector this is
    # postgresql+asyncpg://USER:PASSWORD@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE
    database_url: str = "postgresql+asyncpg://orders:orders@localhost:5432/orders"
    # Keep (pool_size + max_overflow) * max_instances below the database's max_connections.
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    db_echo: bool = False

    payment_gateway: Literal["mock"] = "mock"
    payment_timeout_seconds: float = Field(default=5.0, gt=0)
    payment_mock_latency_seconds: float = Field(default=0.0, ge=0)

    geocoder: Literal["mock"] = "mock"

    idempotency_key_ttl_hours: int = Field(default=24, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
