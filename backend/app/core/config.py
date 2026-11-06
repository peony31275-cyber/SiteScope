"""Typed application settings, read from environment variables with the ``SITESCOPE_`` prefix.

Sources, highest priority first:

1. keyword arguments passed to ``Settings(...)`` (used by tests),
2. process environment variables,
3. ``backend/.env`` (optional, for local development, git-ignored),
4. the defaults declared below.

Settings are validated when they are first loaded, so a misconfigured process fails at startup
instead of misbehaving later.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# This file is <backend>/app/core/config.py, so parents[2] is the backend directory.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Immutable, validated configuration for a SiteScope process."""

    model_config = SettingsConfigDict(
        env_prefix="SITESCOPE_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        frozen=True,
    )

    # Name of the deployment environment; reported in startup logs.
    environment: Environment = "development"

    # Minimum level of emitted log records. Case-insensitive in the environment.
    log_level: LogLevel = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate the settings once per process."""
    return Settings()
