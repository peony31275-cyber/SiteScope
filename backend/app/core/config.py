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

List settings (approved crawl hosts, schemes, ports) are comma-separated in the environment, e.g.
``SITESCOPE_CRAWL_ALLOWED_HOSTS=docs.example.com,*.example.org``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

from app.crawler.destinations import parse_internal_origin
from app.crawler.scope import parse_host_pattern

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
CrawlScheme = Literal["http", "https"]
Port = Annotated[int, Field(ge=1, le=65535)]

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

    # --- Crawl targets and limits ----------------------------------------------------------------
    # Approved crawl targets (policy: app/crawler/scope.py). "docs.example.com" approves exactly
    # that host; "*.example.com" approves its subdomains, not example.com itself. Empty by
    # default: until an operator approves a host, every crawl request is rejected.
    crawl_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    # Schemes and ports (explicit, or the scheme's default) a crawl target may use.
    crawl_allowed_schemes: Annotated[tuple[CrawlScheme, ...], NoDecode, Field(min_length=1)] = (
        "http",
        "https",
    )
    crawl_allowed_ports: Annotated[tuple[Port, ...], NoDecode, Field(min_length=1)] = (80, 443)
    # The largest max_depth and max_pages a crawl job may request.
    crawl_max_depth_limit: int = Field(default=5, ge=0, le=50)
    crawl_max_pages_limit: int = Field(default=1000, ge=1, le=100_000)

    # --- Safe fetcher (app/crawler/fetcher.py) ---------------------------------------------------
    # Seconds allowed for DNS resolution, the TCP connection and the TLS handshake.
    fetch_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Seconds allowed for each read or write of a request and its response.
    fetch_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Seconds allowed for a whole fetch, redirects included (stops slow-drip responses).
    fetch_deadline_seconds: float = Field(default=60.0, gt=0, le=600)
    # The largest response body, after removing gzip or deflate. Larger bodies are rejected.
    fetch_max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    # Redirects followed per fetch; every target is validated again.
    fetch_max_redirects: int = Field(default=5, ge=0, le=20)
    # Exact origins (scheme://host[:port]) that may resolve to private addresses, such as the
    # self-hosted demo site; outside production, also to loopback addresses (local fixture
    # servers). Empty by default. Policy: app/crawler/destinations.py.
    fetch_internal_origins: Annotated[tuple[str, ...], NoDecode] = ()

    # --- HTML parsing (app/crawler/html_parser.py) -----------------------------------------------
    # Distinct links kept per page, and characters of visible text kept per page.
    parse_max_links: int = Field(default=1000, ge=1, le=100_000)
    parse_max_text_chars: int = Field(default=1_000_000, ge=1000, le=1_000_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "crawl_allowed_hosts", "crawl_allowed_ports", "fetch_internal_origins", mode="before"
    )
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        return _split_list(value)

    @field_validator("fetch_internal_origins")
    @classmethod
    def _normalize_internal_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Invalid entries raise ValueError, which fails startup.
        return tuple(dict.fromkeys(str(parse_internal_origin(entry)) for entry in value))

    @field_validator("crawl_allowed_schemes", mode="before")
    @classmethod
    def _split_and_lowercase_schemes(cls, value: object) -> object:
        value = _split_list(value)
        if isinstance(value, tuple | list):
            return tuple(item.lower() if isinstance(item, str) else item for item in value)
        return value

    @field_validator("crawl_allowed_hosts")
    @classmethod
    def _normalize_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Invalid entries raise HostPatternError, a ValueError, which fails startup.
        return tuple(dict.fromkeys(str(parse_host_pattern(entry)) for entry in value))

    @model_validator(mode="after")
    def _reject_development_targets_in_production(self) -> Self:
        if self.environment == "production":
            local = [
                entry
                for entry in self.crawl_allowed_hosts
                if parse_host_pattern(entry).development_only
            ]
            if local:
                raise ValueError(
                    "production does not allow development-only crawl targets"
                    " (localhost, .localhost names, IP addresses): " + ", ".join(local)
                )
            local_origins = [
                entry
                for entry in self.fetch_internal_origins
                if parse_host_pattern(parse_internal_origin(entry).host).development_only
            ]
            if local_origins:
                raise ValueError(
                    "production does not allow development-only internal origins"
                    " (localhost, .localhost names, IP addresses): " + ", ".join(local_origins)
                )
        return self

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


def _split_list(value: object) -> object:
    """Turn a comma-separated environment value into a tuple; leave other values unchanged."""
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate the settings once per process."""
    return Settings()
