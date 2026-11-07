"""Typed application settings, read from environment variables with the ``SITESCOPE_`` prefix.

Sources, highest priority first:

1. keyword arguments passed to ``Settings(...)`` (used by tests),
2. process environment variables,
3. ``backend/.env`` (optional, for local development, git-ignored; backend/compose.yaml reads it
   too, so the containers and the application share one set of credentials),
4. the defaults declared below.

Settings are validated when they are first loaded, so a misconfigured process fails at startup
instead of misbehaving later. In production, the PostgreSQL and Redis passwords are mandatory. In
development they may be missing: the application still starts, and the readiness probe reports
the dependencies as unavailable.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# This file is <backend>/app/core/config.py, so parents[2] is the backend directory.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Values copied unchanged from backend/.env.example; they must never reach production.
_PLACEHOLDER_SECRET_PREFIX = "change-me"  # noqa: S105 - a marker for template values, not a secret


class Settings(BaseSettings):
    """Immutable, validated configuration for a SiteScope process."""

    model_config = SettingsConfigDict(
        env_prefix="SITESCOPE_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        frozen=True,
    )

    # --- General -------------------------------------------------------------------------------
    # Name of the deployment environment; reported in startup logs.
    environment: Environment = "development"
    # Minimum level of emitted log records. Case-insensitive in the environment.
    log_level: LogLevel = "INFO"

    # --- PostgreSQL ----------------------------------------------------------------------------
    postgres_host: str = Field(default="127.0.0.1", min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = Field(default="sitescope", min_length=1)
    postgres_password: SecretStr | None = None
    # Restricted to plain identifiers so that derived names (e.g. the test database) are safe.
    postgres_db: str = Field(default="sitescope", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    db_command_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)

    # --- Redis ---------------------------------------------------------------------------------
    redis_host: str = Field(default="127.0.0.1", min_length=1)
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_password: SecretStr | None = None
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0, le=300)

    # --- Health checks -------------------------------------------------------------------------
    # Maximum seconds each dependency check in the readiness probe may take.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_credentials_in_production(self) -> Self:
        if self.environment == "production":
            problems = [
                name
                for name, secret in self._credentials().items()
                if secret is None
                or not secret.get_secret_value()
                or secret.get_secret_value().startswith(_PLACEHOLDER_SECRET_PREFIX)
            ]
            if problems:
                raise ValueError(
                    "production requires real values for: " + ", ".join(sorted(problems))
                )
        return self

    def _credentials(self) -> dict[str, SecretStr | None]:
        return {
            "SITESCOPE_POSTGRES_PASSWORD": self.postgres_password,
            "SITESCOPE_REDIS_PASSWORD": self.redis_password,
        }

    def missing_credentials(self) -> list[str]:
        """Names of credential settings that are not set (reported as a startup warning)."""
        return sorted(name for name, secret in self._credentials().items() if secret is None)

    @property
    def database_url(self) -> URL:
        """SQLAlchemy connection URL. ``str()`` masks the password; never log it rendered."""
        password = self.postgres_password.get_secret_value() if self.postgres_password else None
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate the settings once per process."""
    return Settings()
