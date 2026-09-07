"""Environment settings, validated once at startup.

Every service calls ``load_settings()`` before it binds a port. A missing or
malformed variable raises here, at boot, rather than on the first request that
happens to need it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "staging", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MMP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    # --- identity -------------------------------------------------------
    environment: Environment = "dev"
    service_name: str = "unknown"
    version: str = "0.1.0"

    # --- data stores ----------------------------------------------------
    database_url: PostgresDsn
    redis_url: RedisDsn

    # Separate pool sizes: the tracker wants a handful of connections and a
    # short statement timeout; the API tolerates slower analytical reads.
    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=10, ge=1)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100)

    # --- http -----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    max_payload_bytes: int = Field(default=256 * 1024, ge=1024)
    max_decompressed_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)

    # --- secrets --------------------------------------------------------
    # Peppers are read from the environment in dev and from KMS in prod; the
    # application only ever sees the resolved value.
    api_key_pepper: str = Field(min_length=32)
    ip_hash_pepper: str = Field(min_length=32)
    session_secret: str = Field(min_length=32)

    # --- observability --------------------------------------------------
    shutdown_grace_seconds: float = Field(default=5.0, ge=0.0)

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_json: bool = True

    @field_validator("api_key_pepper", "ip_hash_pepper", "session_secret")
    @classmethod
    def _reject_placeholder_secrets(cls, v: str, info: object) -> str:
        lowered = v.lower()
        if lowered.startswith(("change", "placeholder", "todo", "xxx")):
            raise ValueError("placeholder secret rejected — set a real value")
        return v

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    def asyncpg_dsn(self) -> str:
        """asyncpg wants a plain postgres:// DSN without the driver suffix."""
        return str(self.database_url).replace("postgresql+asyncpg://", "postgresql://")


@lru_cache(maxsize=1)
def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
