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

# The default margin: a crawl task stops this many seconds before its lease expires (ADR-0003:
# task_deadline < lease_duration). Configurable as SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS.
LEASE_MARGIN_SECONDS = 10.0

# Values copied unchanged from backend/.env.example; they must never reach production.
_PLACEHOLDER_SECRET_PREFIX = "change-me"  # noqa: S105 - a marker for template values, not a secret

# The only client errors that SITESCOPE_RETRY_HTTP_STATUSES may list: Request Timeout, Too Early
# and Too Many Requests. Every other 4xx status (404 and 410 above all) is permanent.
RETRYABLE_CLIENT_ERRORS = frozenset({408, 425, 429})


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

    # --- Crawling (Phase 4) ---------------------------------------------------------------------
    # The crawler's name (RFC 9309 "product token"): sent as "<name>/<version>" in User-Agent,
    # and matched case-insensitively against robots.txt user-agent lines and meta robots names.
    crawler_name: str = Field(default="SiteScopeBot", pattern=r"^[A-Za-z_-]{1,40}$")
    # URLs one crawl process (a crawler worker, or the single-process driver) processes at once.
    crawl_concurrency: int = Field(default=4, ge=1, le=64)
    # How long a claimed URL stays leased (by the PostgreSQL clock). A task is cut off
    # SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS before its lease ends, so it never runs on an expired
    # lease (task_deadline < lease_duration, ADR-0003).
    crawl_lease_seconds: float = Field(default=90.0, gt=0, le=3600)
    # The task deadline is the lease minus this margin, which leaves time to commit the result.
    crawl_lease_margin_seconds: float = Field(default=LEASE_MARGIN_SECONDS, gt=0, le=60)
    # Claims of one URL, retries and crashed attempts included, before its last failure is
    # final (or, after crashes, it fails with max_attempts_exceeded).
    crawl_max_attempts: int = Field(default=3, ge=1, le=10)
    # How often a crawler worker checks whether the jobs of its running tasks were cancelled
    # (it then stops those tasks and records them as cancelled).
    crawl_cancel_check_seconds: float = Field(default=2.0, gt=0, le=60)

    # --- Retries (Phase 6) -----------------------------------------------------------------------
    # A retryable failure (temporary DNS failure, connection failure, timeout, truncated
    # response, rate_limited, deadline_exceeded, a status in SITESCOPE_RETRY_HTTP_STATUSES) is
    # tried again while attempts remain. The n-th retry waits a random time between 0 and
    # min(SITESCOPE_RETRY_MAX_DELAY_SECONDS, SITESCOPE_RETRY_BASE_DELAY_SECONDS * 2^(n-1)),
    # and at least what a valid Retry-After asks for.
    retry_base_delay_seconds: float = Field(default=2.0, gt=0, le=3600)
    retry_max_delay_seconds: float = Field(default=60.0, gt=0, le=86_400)
    # A Retry-After (HTTP 429 or 503) longer than this is not waited for: the URL is not retried.
    retry_after_max_seconds: float = Field(default=300.0, ge=0, le=86_400)
    # HTTP statuses retried: server errors (500-599), and of the client errors only 408, 425 and
    # 429 (RETRYABLE_CLIENT_ERRORS). Any other HTTP status is final; empty retries none.
    retry_http_statuses: Annotated[tuple[int, ...], NoDecode] = (429, 500, 502, 503, 504)

    # --- Redis Streams task delivery (app/queue, Phase 5) ----------------------------------------
    # How long a worker's XREADGROUP blocks waiting for tasks (below the Redis socket timeout).
    stream_read_block_ms: int = Field(default=2000, ge=10, le=60_000)
    # How often a worker looks for tasks abandoned by crashed or stalled workers (XAUTOCLAIM),
    # and how long a task must have been idle first. The idle time must be at least the lease,
    # so a task is never taken from a worker that still holds its lease.
    stream_reclaim_interval_seconds: float = Field(default=15.0, gt=0, le=3600)
    stream_reclaim_min_idle_seconds: float = Field(default=100.0, gt=0, le=86_400)
    stream_reclaim_batch_size: int = Field(default=50, ge=1, le=1000)
    # A message delivered more often without being settled, whose URL can no longer be claimed
    # (its attempts are used up), is poison: the URL is failed (max_deliveries_exceeded) and the
    # message is dead-lettered (Phase 6). A URL that a claim can take is claimed as usual.
    stream_max_deliveries: int = Field(default=10, ge=2, le=1000)
    # Delivered tasks older than this are trimmed from the stream; tasks still pending in a
    # consumer group, and tasks not yet delivered, are never trimmed.
    stream_retention_seconds: int = Field(default=3600, ge=60, le=7 * 86_400)
    # Consumers (one per worker process start) with nothing pending are removed after this idle
    # time.
    stream_consumer_max_idle_seconds: int = Field(default=3600, ge=60, le=7 * 86_400)

    # --- Scheduler (app/scheduler, Phase 5) ------------------------------------------------------
    # Pause between two scheduler passes (activation, level advancement, dispatch).
    scheduler_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    # Tasks published per job and pass.
    scheduler_dispatch_batch_size: int = Field(default=100, ge=1, le=10_000)
    # Tasks of one job queued or in progress at a time, and of all jobs together (backpressure).
    scheduler_max_in_flight_per_job: int = Field(default=100, ge=1, le=100_000)
    scheduler_max_outstanding: int = Field(default=1000, ge=1, le=1_000_000)
    # Jobs running at the same time; the oldest queued jobs start first.
    scheduler_max_running_jobs: int = Field(default=10, ge=1, le=10_000)
    # A queued task, or an expired lease, that has made no progress this long is published
    # again (its message may have been lost from Redis).
    scheduler_reconcile_after_seconds: float = Field(default=300.0, gt=0, le=86_400)

    # --- API (Phase 6) ---------------------------------------------------------------------------
    # How long an Idempotency-Key of POST /api/v1/crawls is remembered. Afterwards the key is
    # forgotten (the job stays) and may create a new job.
    idempotency_key_ttl_seconds: int = Field(default=86_400, ge=60, le=30 * 86_400)

    # --- Process shutdown ------------------------------------------------------------------------
    # On SIGINT or SIGTERM, how long a worker lets its running tasks finish before it stops them.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=3600)

    # --- Politeness (app/crawler/politeness.py, app/crawler/robots.py) ---------------------------
    # Minimum seconds between the starts of two requests to one host, shared by every crawler
    # process through Redis. 0 disables the spacing (tests and local benchmarks only).
    politeness_interval_seconds: float = Field(default=1.0, ge=0, le=3600)
    # Requests in flight to one host at the same time, across all crawler processes.
    politeness_max_concurrent_per_host: int = Field(default=1, ge=1, le=16)
    # The longest a request waits for its slot; after that it fails as rate_limited.
    politeness_max_wait_seconds: float = Field(default=30.0, gt=0, le=600)
    # A slot is a lease of this many seconds, renewed by its holder while the request runs, so
    # the slot of a crashed process frees after at most this long (Phase 6). A live holder never
    # keeps a slot longer than the fetch deadline plus 10 s.
    politeness_slot_lease_seconds: float = Field(default=15.0, ge=1, le=600)
    # How long a robots.txt result is cached in Redis (RFC 9309: at most 24 hours), and how long
    # an unreachable robots.txt (5xx, 429, network error: complete disallow) is cached before
    # the next attempt.
    robots_cache_seconds: int = Field(default=86_400, ge=60, le=86_400)
    robots_retry_seconds: int = Field(default=300, ge=10, le=86_400)
    # robots.txt Crawl-delay raises the host's interval up to this cap. It must not exceed
    # SITESCOPE_POLITENESS_MAX_WAIT_SECONDS, or every request would wait past its limit.
    robots_max_crawl_delay_seconds: float = Field(default=10.0, ge=0, le=600)

    # --- Redis keys ------------------------------------------------------------------------------
    # Prefix of every Redis key SiteScope writes (tests use their own prefix to stay isolated).
    redis_key_prefix: str = Field(default="sitescope", pattern=r"^[a-z0-9_-]{1,40}$")

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "crawl_allowed_hosts",
        "crawl_allowed_ports",
        "fetch_internal_origins",
        "retry_http_statuses",
        mode="before",
    )
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        return _split_list(value)

    @field_validator("retry_http_statuses")
    @classmethod
    def _check_retry_statuses(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        # A client error is permanent (404 and 410 above all), except a request timeout (408),
        # a request sent too early (425) and too many requests (429).
        if any(not (500 <= status <= 599 or status in RETRYABLE_CLIENT_ERRORS) for status in value):
            raise ValueError(
                "retryable HTTP statuses must be server errors (500-599), 408, 425 or 429"
            )
        return tuple(sorted(set(value)))

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
    def _check_crawl_timing(self) -> Self:
        # The timing order of architecture section 10.2:
        #   task_deadline < lease_duration <= reclaim_min_idle <= reconcile_after.
        # A task (robots.txt check, politeness wait, fetch, parse) is cut off margin seconds
        # before its lease ends; a page fetch must fit in what remains.
        margin = self.crawl_lease_margin_seconds
        if self.crawl_lease_seconds < self.fetch_deadline_seconds + margin:
            raise ValueError(
                "SITESCOPE_CRAWL_LEASE_SECONDS must be at least SITESCOPE_FETCH_DEADLINE_SECONDS"
                f" + SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS ({margin:g})"
            )
        # A task is reclaimed from another worker only after its lease has expired.
        if self.stream_reclaim_min_idle_seconds < self.crawl_lease_seconds:
            raise ValueError(
                "SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS must be at least"
                " SITESCOPE_CRAWL_LEASE_SECONDS"
            )
        if self.scheduler_reconcile_after_seconds < self.stream_reclaim_min_idle_seconds:
            raise ValueError(
                "SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS must be at least"
                " SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS"
            )
        if self.stream_retention_seconds < self.scheduler_reconcile_after_seconds:
            raise ValueError(
                "SITESCOPE_STREAM_RETENTION_SECONDS must be at least"
                " SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS"
            )
        # A blocking read must return before the Redis client gives up on the socket.
        if self.stream_read_block_ms >= self.redis_socket_timeout_seconds * 1000:
            raise ValueError(
                "SITESCOPE_STREAM_READ_BLOCK_MS must be shorter than"
                " SITESCOPE_REDIS_SOCKET_TIMEOUT_SECONDS"
            )
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError(
                "SITESCOPE_RETRY_BASE_DELAY_SECONDS must not exceed"
                " SITESCOPE_RETRY_MAX_DELAY_SECONDS"
            )
        # A request that finds its host idle may still have to wait one interval.
        if self.politeness_interval_seconds > self.politeness_max_wait_seconds:
            raise ValueError(
                "SITESCOPE_POLITENESS_INTERVAL_SECONDS must not exceed"
                " SITESCOPE_POLITENESS_MAX_WAIT_SECONDS"
            )
        if self.robots_max_crawl_delay_seconds > self.politeness_max_wait_seconds:
            raise ValueError(
                "SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS must not exceed"
                " SITESCOPE_POLITENESS_MAX_WAIT_SECONDS"
            )
        return self

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
