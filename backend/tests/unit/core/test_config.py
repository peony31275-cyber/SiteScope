from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import ENV_FILE, Settings, get_settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.log_level == "INFO"


def test_reads_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_ENVIRONMENT", "production")
    monkeypatch.setenv("SITESCOPE_LOG_LEVEL", "WARNING")
    # Production requires real credentials (Phase 2B).
    monkeypatch.setenv("SITESCOPE_POSTGRES_PASSWORD", "unit-test-postgres-secret")
    monkeypatch.setenv("SITESCOPE_REDIS_PASSWORD", "unit-test-redis-secret")

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "WARNING"


def test_ignores_variables_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert Settings(_env_file=None).log_level == "INFO"


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_LOG_LEVEL", "debug")

    assert Settings(_env_file=None).log_level == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [("SITESCOPE_ENVIRONMENT", "staging"), ("SITESCOPE_LOG_LEVEL", "LOUD")],
)
def test_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SITESCOPE_LOG_LEVEL=ERROR\n", encoding="utf-8")

    assert Settings(_env_file=env_file).log_level == "ERROR"


def test_environment_variable_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SITESCOPE_LOG_LEVEL=ERROR\n", encoding="utf-8")
    monkeypatch.setenv("SITESCOPE_LOG_LEVEL", "DEBUG")

    assert Settings(_env_file=env_file).log_level == "DEBUG"


@pytest.mark.parametrize(
    "line",
    ["SITESCOPE_LOG_LEVLE=DEBUG", "UNRELATED_TOOL_SETTING=1"],
    ids=["misspelled-setting", "unrelated-key"],
)
def test_rejects_unknown_keys_in_env_file(tmp_path: Path, line: str) -> None:
    """backend/.env may contain only known SITESCOPE_ settings, so typos fail at startup."""
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_missing_env_file_is_ignored(tmp_path: Path) -> None:
    assert Settings(_env_file=tmp_path / "does-not-exist.env").log_level == "INFO"


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_get_settings_returns_one_cached_instance() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


# --- PostgreSQL and Redis settings (Phase 2B) ---------------------------------------------------


def test_connection_defaults_target_local_containers() -> None:
    settings = Settings(_env_file=None)

    assert (settings.postgres_host, settings.postgres_port) == ("127.0.0.1", 5432)
    assert (settings.redis_host, settings.redis_port, settings.redis_db) == ("127.0.0.1", 6379, 0)
    assert settings.postgres_password is None
    assert settings.missing_credentials() == [
        "SITESCOPE_POSTGRES_PASSWORD",
        "SITESCOPE_REDIS_PASSWORD",
    ]


def test_database_url_escapes_and_masks_the_password() -> None:
    settings = Settings(
        _env_file=None, postgres_password=SecretStr("p@ss:w/rd#1"), postgres_db="sitescope"
    )

    url = settings.database_url

    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "p@ss:w/rd#1"
    assert "p@ss" not in str(url)  # str() masks the password
    rendered = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd%231@127.0.0.1:5432/sitescope" in rendered


def test_secrets_are_masked_in_settings_repr() -> None:
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret"),
        redis_password=SecretStr("rd-secret"),
    )

    assert "pg-secret" not in repr(settings)
    assert "rd-secret" not in repr(settings)


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"postgres_password": SecretStr("real-postgres-secret")},
        {
            "postgres_password": SecretStr("change-me-local-postgres-password"),
            "redis_password": SecretStr("real-redis"),
        },
    ],
    ids=["none", "redis-missing", "placeholder"],
)
def test_production_requires_real_credentials(credentials: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="production requires real values"):
        Settings(_env_file=None, environment="production", **credentials)


def test_production_accepts_real_credentials() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        postgres_password=SecretStr("real-postgres-secret"),
        redis_password=SecretStr("real-redis-secret"),
    )

    assert settings.missing_credentials() == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_POSTGRES_PORT", "0"),
        ("SITESCOPE_REDIS_PORT", "70000"),
        ("SITESCOPE_REDIS_DB", "16"),
        ("SITESCOPE_DB_POOL_SIZE", "0"),
        ("SITESCOPE_READINESS_TIMEOUT_SECONDS", "0"),
        ("SITESCOPE_POSTGRES_DB", "bad name; drop"),
    ],
)
def test_rejects_out_of_range_connection_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- Crawl targets and limits -----------------------------------------------------------------


def test_crawl_defaults_approve_no_target() -> None:
    settings = Settings(_env_file=None)

    assert settings.crawl_allowed_hosts == ()
    assert settings.crawl_allowed_schemes == ("http", "https")
    assert settings.crawl_allowed_ports == (80, 443)
    assert (settings.crawl_max_depth_limit, settings.crawl_max_pages_limit) == (5, 1000)


def test_crawl_lists_are_comma_separated_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SITESCOPE_CRAWL_ALLOWED_HOSTS",
        " Docs.Example.COM , *.Example.org,,bücher.example,docs.example.com",
    )
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_SCHEMES", "HTTPS")
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_PORTS", "443, 8443")

    settings = Settings(_env_file=None)

    # Normalized like URL hosts, duplicates removed, order kept.
    assert settings.crawl_allowed_hosts == (
        "docs.example.com",
        "*.example.org",
        "xn--bcher-kva.example",
    )
    assert settings.crawl_allowed_schemes == ("https",)
    assert settings.crawl_allowed_ports == (443, 8443)


def test_an_empty_host_list_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_HOSTS", "")

    assert Settings(_env_file=None).crawl_allowed_hosts == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_CRAWL_ALLOWED_HOSTS", "*.com"),
        ("SITESCOPE_CRAWL_ALLOWED_HOSTS", "https://docs.example.com"),
        ("SITESCOPE_CRAWL_ALLOWED_HOSTS", "docs.example.com:8080"),
        ("SITESCOPE_CRAWL_ALLOWED_HOSTS", "169.254.169.254"),
        ("SITESCOPE_CRAWL_ALLOWED_SCHEMES", "ftp"),
        ("SITESCOPE_CRAWL_ALLOWED_SCHEMES", ""),
        ("SITESCOPE_CRAWL_ALLOWED_PORTS", "0"),
        ("SITESCOPE_CRAWL_ALLOWED_PORTS", "http"),
        ("SITESCOPE_CRAWL_ALLOWED_PORTS", ""),
        ("SITESCOPE_CRAWL_MAX_DEPTH_LIMIT", "51"),
        ("SITESCOPE_CRAWL_MAX_DEPTH_LIMIT", "-1"),
        ("SITESCOPE_CRAWL_MAX_PAGES_LIMIT", "0"),
        ("SITESCOPE_CRAWL_MAX_PAGES_LIMIT", "100001"),
    ],
)
def test_rejects_invalid_crawl_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "hosts", ["localhost", "app.localhost", "127.0.0.1", "[::1]", "docs.example.com,192.0.2.10"]
)
def test_production_refuses_development_only_crawl_targets(
    monkeypatch: pytest.MonkeyPatch, hosts: str
) -> None:
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_HOSTS", hosts)

    with pytest.raises(ValidationError, match="development-only crawl targets"):
        Settings(
            _env_file=None,
            environment="production",
            postgres_password=SecretStr("real-postgres-secret"),
            redis_password=SecretStr("real-redis-secret"),
        )


def test_development_allows_local_crawl_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")

    settings = Settings(_env_file=None)

    assert settings.crawl_allowed_hosts == ("localhost", "127.0.0.1", "[::1]")


def test_production_accepts_public_crawl_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_CRAWL_ALLOWED_HOSTS", "docs.example.com,*.example.org,demo-site")

    settings = Settings(
        _env_file=None,
        environment="production",
        postgres_password=SecretStr("real-postgres-secret"),
        redis_password=SecretStr("real-redis-secret"),
    )

    assert settings.crawl_allowed_hosts == ("docs.example.com", "*.example.org", "demo-site")


def test_env_example_documents_every_setting_with_valid_values() -> None:
    """backend/.env.example must load, and must list every setting (CLAUDE.md, Configuration)."""
    template = ENV_FILE.parent / ".env.example"
    keys = {
        line.split("=", 1)[0]
        for line in template.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    Settings(_env_file=template)
    assert keys == {f"SITESCOPE_{name.upper()}" for name in Settings.model_fields}


# --- Safe fetcher and parser ------------------------------------------------------------------


def test_fetch_and_parse_defaults() -> None:
    settings = Settings(_env_file=None)

    assert (settings.fetch_connect_timeout_seconds, settings.fetch_read_timeout_seconds) == (10, 10)
    assert (settings.fetch_deadline_seconds, settings.fetch_max_redirects) == (60, 5)
    assert settings.fetch_max_body_bytes == 2 * 1024 * 1024
    assert settings.fetch_internal_origins == ()
    assert (settings.parse_max_links, settings.parse_max_text_chars) == (1000, 1_000_000)


def test_internal_origins_are_comma_separated_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SITESCOPE_FETCH_INTERNAL_ORIGINS",
        " HTTP://Demo-Site:80/ , http://localhost:8080,http://demo-site",
    )

    settings = Settings(_env_file=None)

    assert settings.fetch_internal_origins == ("http://demo-site", "http://localhost:8080")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_FETCH_INTERNAL_ORIGINS", "demo-site:8080"),
        ("SITESCOPE_FETCH_INTERNAL_ORIGINS", "http://demo-site/path"),
        ("SITESCOPE_FETCH_INTERNAL_ORIGINS", "ftp://demo-site"),
        ("SITESCOPE_FETCH_CONNECT_TIMEOUT_SECONDS", "0"),
        ("SITESCOPE_FETCH_READ_TIMEOUT_SECONDS", "121"),
        ("SITESCOPE_FETCH_DEADLINE_SECONDS", "601"),
        ("SITESCOPE_FETCH_MAX_BODY_BYTES", "1023"),
        ("SITESCOPE_FETCH_MAX_BODY_BYTES", str(10 * 1024 * 1024 + 1)),
        ("SITESCOPE_FETCH_MAX_REDIRECTS", "21"),
        ("SITESCOPE_FETCH_MAX_REDIRECTS", "-1"),
        ("SITESCOPE_PARSE_MAX_LINKS", "0"),
        ("SITESCOPE_PARSE_MAX_TEXT_CHARS", "1000001"),
    ],
)
def test_rejects_invalid_fetch_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "origins",
    ["http://localhost:8080", "http://127.0.0.1:8000", "http://[::1]", "http://a.localhost"],
)
def test_production_refuses_local_internal_origins(
    monkeypatch: pytest.MonkeyPatch, origins: str
) -> None:
    monkeypatch.setenv("SITESCOPE_FETCH_INTERNAL_ORIGINS", origins)

    with pytest.raises(ValidationError, match="development-only internal origins"):
        Settings(
            _env_file=None,
            environment="production",
            postgres_password=SecretStr("real-postgres-secret"),
            redis_password=SecretStr("real-redis-secret"),
        )


def test_production_accepts_named_internal_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_FETCH_INTERNAL_ORIGINS", "http://demo-site:8080")

    settings = Settings(
        _env_file=None,
        environment="production",
        postgres_password=SecretStr("real-postgres-secret"),
        redis_password=SecretStr("real-redis-secret"),
    )

    assert settings.fetch_internal_origins == ("http://demo-site:8080",)


# --- Crawling and politeness (Phase 4) ---------------------------------------------------------


def test_crawl_and_politeness_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.crawler_name == "SiteScopeBot"
    assert (settings.crawl_concurrency, settings.crawl_max_attempts) == (4, 3)
    assert settings.crawl_lease_seconds == 90
    assert (settings.politeness_interval_seconds, settings.politeness_max_wait_seconds) == (1, 30)
    assert settings.politeness_max_concurrent_per_host == 1
    assert (settings.robots_cache_seconds, settings.robots_retry_seconds) == (86_400, 300)
    assert settings.robots_max_crawl_delay_seconds == 10
    assert settings.redis_key_prefix == "sitescope"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_CRAWLER_NAME", "SiteScope Bot"),  # RFC 9309 product token characters only
        ("SITESCOPE_CRAWLER_NAME", "SiteScopeBot/1.0"),
        ("SITESCOPE_CRAWLER_NAME", ""),
        ("SITESCOPE_CRAWL_CONCURRENCY", "0"),
        ("SITESCOPE_CRAWL_MAX_ATTEMPTS", "0"),
        ("SITESCOPE_POLITENESS_INTERVAL_SECONDS", "-1"),
        ("SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST", "0"),
        ("SITESCOPE_POLITENESS_MAX_WAIT_SECONDS", "0"),
        ("SITESCOPE_ROBOTS_CACHE_SECONDS", "86401"),  # RFC 9309: at most 24 hours
        ("SITESCOPE_ROBOTS_CACHE_SECONDS", "59"),
        ("SITESCOPE_ROBOTS_RETRY_SECONDS", "9"),
        ("SITESCOPE_REDIS_KEY_PREFIX", "Upper"),
        ("SITESCOPE_REDIS_KEY_PREFIX", "a:b"),
    ],
)
def test_rejects_invalid_crawl_and_politeness_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"SITESCOPE_FETCH_DEADLINE_SECONDS": "60", "SITESCOPE_CRAWL_LEASE_SECONDS": "69"},
            "SITESCOPE_CRAWL_LEASE_SECONDS must be at least",
        ),
        (
            {"SITESCOPE_POLITENESS_INTERVAL_SECONDS": "31"},
            "SITESCOPE_POLITENESS_INTERVAL_SECONDS must not exceed",
        ),
        (
            {"SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS": "31"},
            "SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS must not exceed",
        ),
    ],
)
def test_rejects_inconsistent_crawl_timing(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], message: str
) -> None:
    """The task deadline ends before the lease (ADR-0003); waits fit the politeness limit."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)


def test_accepts_a_lease_exactly_ten_seconds_longer_than_the_fetch_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITESCOPE_FETCH_DEADLINE_SECONDS", "30")
    monkeypatch.setenv("SITESCOPE_CRAWL_LEASE_SECONDS", "40")

    assert Settings(_env_file=None).crawl_lease_seconds == 40


# --- Redis Streams, scheduler, shutdown (Phase 5) ------------------------------------------------


def test_stream_and_scheduler_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.crawl_lease_margin_seconds == 10
    assert settings.stream_read_block_ms == 2000
    assert settings.stream_reclaim_interval_seconds == 15
    assert settings.stream_reclaim_min_idle_seconds == 100  # 120 until Phase 6
    assert settings.stream_reclaim_batch_size == 50
    assert settings.stream_retention_seconds == 3600
    assert settings.stream_consumer_max_idle_seconds == 3600
    assert settings.scheduler_interval_seconds == 0.5
    assert settings.scheduler_dispatch_batch_size == 100
    assert settings.scheduler_max_in_flight_per_job == 100
    assert settings.scheduler_tasks_per_host_slot == 2  # Phase 10
    assert settings.scheduler_max_outstanding == 1000
    assert settings.scheduler_max_running_jobs == 10
    assert settings.scheduler_reconcile_after_seconds == 300  # 600 until Phase 6
    assert settings.shutdown_grace_seconds == 30
    assert settings.heartbeat_interval_seconds == 10  # Phase 10


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS", "0"),
        ("SITESCOPE_CRAWL_CONCURRENCY", "65"),
        ("SITESCOPE_STREAM_READ_BLOCK_MS", "9"),
        ("SITESCOPE_STREAM_RECLAIM_INTERVAL_SECONDS", "0"),
        ("SITESCOPE_STREAM_RECLAIM_BATCH_SIZE", "0"),
        ("SITESCOPE_STREAM_RETENTION_SECONDS", "59"),
        ("SITESCOPE_STREAM_CONSUMER_MAX_IDLE_SECONDS", "59"),
        ("SITESCOPE_SCHEDULER_INTERVAL_SECONDS", "0"),
        ("SITESCOPE_SCHEDULER_DISPATCH_BATCH_SIZE", "0"),
        ("SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB", "0"),
        ("SITESCOPE_SCHEDULER_MAX_OUTSTANDING", "0"),
        ("SITESCOPE_SCHEDULER_MAX_RUNNING_JOBS", "0"),
        ("SITESCOPE_SCHEDULER_TASKS_PER_HOST_SLOT", "0"),
        ("SITESCOPE_SHUTDOWN_GRACE_SECONDS", "-1"),
        ("SITESCOPE_HEARTBEAT_INTERVAL_SECONDS", "0.5"),
        ("SITESCOPE_HEARTBEAT_INTERVAL_SECONDS", "301"),
    ],
)
def test_rejects_invalid_stream_and_scheduler_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (  # a custom margin counts, not the default one
            {
                "SITESCOPE_FETCH_DEADLINE_SECONDS": "60",
                "SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS": "20",
                "SITESCOPE_CRAWL_LEASE_SECONDS": "79",
            },
            "SITESCOPE_CRAWL_LEASE_SECONDS must be at least",
        ),
        (  # a message must never be taken from a worker that may still hold its lease
            {"SITESCOPE_CRAWL_LEASE_SECONDS": "121"},
            "SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS must be at least",
        ),
        (  # one second below the default reclaim idle time (100 s)
            {"SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS": "99"},
            "SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS must be at least",
        ),
        (  # one second below the default reconcile time (300 s)
            {"SITESCOPE_STREAM_RETENTION_SECONDS": "299"},
            "SITESCOPE_STREAM_RETENTION_SECONDS must be at least",
        ),
        (  # a blocking read must return before the Redis client gives up on the socket
            {"SITESCOPE_STREAM_READ_BLOCK_MS": "5000"},
            "SITESCOPE_STREAM_READ_BLOCK_MS must be shorter than",
        ),
    ],
)
def test_rejects_inconsistent_stream_timing(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], message: str
) -> None:
    """task_deadline < lease <= reclaim min idle <= reconcile after <= retention (ADR-0016)."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)


def test_accepts_the_fast_timing_of_the_process_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The short timings used by the multi-process tests satisfy the same order."""
    values = {
        "SITESCOPE_FETCH_DEADLINE_SECONDS": "3",
        "SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS": "1",
        "SITESCOPE_CRAWL_LEASE_SECONDS": "4",
        "SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS": "4",
        "SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS": "4",
        "SITESCOPE_STREAM_RETENTION_SECONDS": "60",
        "SITESCOPE_POLITENESS_MAX_WAIT_SECONDS": "3",
        "SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS": "1",
        # Phase 7: the index pipeline's own order (task deadline + 2 <= reclaim <= reconcile).
        "SITESCOPE_INDEX_TASK_DEADLINE_SECONDS": "3",
        "SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS": "5",
        "SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS": "10",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.crawl_lease_seconds - settings.crawl_lease_margin_seconds == 3


# --- Retries, poison messages, cancellation, idempotency (Phase 6) ------------------------------


def test_phase_6_defaults() -> None:
    settings = Settings(_env_file=None)

    assert (settings.retry_base_delay_seconds, settings.retry_max_delay_seconds) == (2, 60)
    assert settings.retry_after_max_seconds == 300
    assert settings.retry_http_statuses == (429, 500, 502, 503, 504)
    assert settings.stream_max_deliveries == 10
    assert settings.crawl_cancel_check_seconds == 2
    assert settings.idempotency_key_ttl_seconds == 86_400
    assert settings.politeness_slot_lease_seconds == 15


def test_retry_statuses_are_comma_separated_sorted_and_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITESCOPE_RETRY_HTTP_STATUSES", " 503, 429,408,503 ,500")

    assert Settings(_env_file=None).retry_http_statuses == (408, 429, 500, 503)


def test_an_empty_retry_status_list_retries_no_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITESCOPE_RETRY_HTTP_STATUSES", "")

    assert Settings(_env_file=None).retry_http_statuses == ()


@pytest.mark.parametrize("statuses", ["404", "410", "429,410", "400", "403", "399", "600", "x"])
def test_permanent_http_statuses_cannot_be_retried(
    monkeypatch: pytest.MonkeyPatch, statuses: str
) -> None:
    """404 and 410 (and every client error but 408, 425 and 429) are always final."""
    monkeypatch.setenv("SITESCOPE_RETRY_HTTP_STATUSES", statuses)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_RETRY_BASE_DELAY_SECONDS", "0"),
        ("SITESCOPE_RETRY_MAX_DELAY_SECONDS", "0"),
        ("SITESCOPE_RETRY_AFTER_MAX_SECONDS", "-1"),
        ("SITESCOPE_STREAM_MAX_DELIVERIES", "1"),  # one delivery is no evidence of poison
        ("SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS", "0"),
        ("SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS", "61"),
        ("SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS", "59"),
        ("SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS", "0.5"),
    ],
)
def test_rejects_invalid_phase_6_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_base_retry_delay_must_not_exceed_the_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITESCOPE_RETRY_BASE_DELAY_SECONDS", "61")

    with pytest.raises(ValidationError, match="SITESCOPE_RETRY_BASE_DELAY_SECONDS must not exceed"):
        Settings(_env_file=None)


# --- Indexing and search (Phase 7) ---------------------------------------------------------------


def test_phase_7_defaults() -> None:
    settings = Settings(_env_file=None)

    assert (settings.index_concurrency, settings.index_max_attempts) == (4, 3)
    assert settings.index_max_body_chars == 100_000
    assert settings.index_task_deadline_seconds == 20
    assert settings.index_reclaim_min_idle_seconds == 60
    assert settings.index_reconcile_after_seconds == 300
    assert (settings.index_dispatch_batch_size, settings.index_max_outstanding) == (200, 1000)
    assert settings.index_dispatcher_interval_seconds == 0.5
    assert settings.search_timeout_seconds == 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SITESCOPE_INDEX_CONCURRENCY", "0"),
        ("SITESCOPE_INDEX_CONCURRENCY", "65"),
        ("SITESCOPE_INDEX_MAX_ATTEMPTS", "0"),
        ("SITESCOPE_INDEX_MAX_ATTEMPTS", "11"),
        ("SITESCOPE_INDEX_MAX_BODY_CHARS", "999"),
        ("SITESCOPE_INDEX_MAX_BODY_CHARS", "150001"),  # beyond what fits a tsvector safely
        ("SITESCOPE_INDEX_TASK_DEADLINE_SECONDS", "0"),
        ("SITESCOPE_INDEX_DISPATCH_BATCH_SIZE", "0"),
        ("SITESCOPE_INDEX_MAX_OUTSTANDING", "0"),
        ("SITESCOPE_INDEX_DISPATCHER_INTERVAL_SECONDS", "0"),
        ("SITESCOPE_SEARCH_TIMEOUT_SECONDS", "0"),
        ("SITESCOPE_SEARCH_TIMEOUT_SECONDS", "61"),
    ],
)
def test_rejects_invalid_phase_7_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (  # PostgreSQL must cut a statement off before the client gives up on it
            {"SITESCOPE_INDEX_TASK_DEADLINE_SECONDS": "30"},
            "SITESCOPE_INDEX_TASK_DEADLINE_SECONDS must be shorter than",
        ),
        (  # an attempt's lease must outlast the attempt by at least 1 s (independent review)
            {"SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS": "21.9"},
            "SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS must be at least",
        ),
        (  # one second below the default reclaim idle time (60 s)
            {"SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS": "59"},
            "SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS must be at least",
        ),
        (  # the stream keeps an entry at least until the reconciler would replace it
            {
                "SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS": "400",
                "SITESCOPE_STREAM_RETENTION_SECONDS": "399",
            },
            "SITESCOPE_STREAM_RETENTION_SECONDS must be at least",
        ),
        (  # one database connection per index task (independent review)
            {"SITESCOPE_INDEX_CONCURRENCY": "11"},
            "SITESCOPE_INDEX_CONCURRENCY must not exceed",
        ),
        (  # PostgreSQL and the service stop a search before the client gives up on it
            {"SITESCOPE_SEARCH_TIMEOUT_SECONDS": "30"},
            "SITESCOPE_SEARCH_TIMEOUT_SECONDS must be shorter than",
        ),
    ],
)
def test_rejects_inconsistent_index_timing(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], message: str
) -> None:
    """task deadline < reclaim min idle <= reconcile after <= retention, and the deadline is
    shorter than the database command timeout (ADR-0018)."""
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)


def test_accepts_the_boundaries_of_the_index_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS": "22",  # exactly the deadline (20) + 2
        "SITESCOPE_INDEX_CONCURRENCY": "10",  # exactly the pool: 5 + 5
        "SITESCOPE_SEARCH_TIMEOUT_SECONDS": "29.9",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert (settings.index_reclaim_min_idle_seconds, settings.index_concurrency) == (22, 10)


# --- Website analysis (Phase 8) ------------------------------------------------------------------


def test_phase_8_defaults() -> None:
    assert Settings(_env_file=None).analysis_timeout_seconds == 10


@pytest.mark.parametrize("value", ["0", "-1", "61"])
def test_rejects_an_invalid_analysis_timeout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SITESCOPE_ANALYSIS_TIMEOUT_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_postgresql_stops_an_analysis_before_the_client_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SITESCOPE_ANALYSIS_TIMEOUT_SECONDS", "30")

    with pytest.raises(
        ValidationError, match="SITESCOPE_ANALYSIS_TIMEOUT_SECONDS must be shorter than"
    ):
        Settings(_env_file=None)
    monkeypatch.setenv("SITESCOPE_ANALYSIS_TIMEOUT_SECONDS", "29.9")
    assert Settings(_env_file=None).analysis_timeout_seconds == 29.9


# --- API writes and job limits (Phase 11) --------------------------------------------------------

PRODUCTION_CREDENTIALS: dict[str, Any] = {
    "postgres_password": SecretStr("real-postgres-secret"),
    "redis_password": SecretStr("real-redis-secret"),
}


def test_phase_11_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.admin_token is None
    assert settings.crawl_max_active_jobs == 50
    assert settings.write_access == "open"  # development, no token


def test_write_access_follows_the_environment_and_the_token() -> None:
    token = SecretStr("t" * 32)

    assert Settings(_env_file=None, environment="test").write_access == "open"
    assert Settings(_env_file=None, environment="test", admin_token=token).write_access == "token"
    production = Settings(_env_file=None, environment="production", **PRODUCTION_CREDENTIALS)
    assert production.write_access == "disabled"  # safe by default: read-only
    with_token = Settings(
        _env_file=None, environment="production", admin_token=token, **PRODUCTION_CREDENTIALS
    )
    assert with_token.write_access == "token"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_admin_token_is_no_token(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SITESCOPE_ADMIN_TOKEN", value)

    settings = Settings(_env_file=None)

    assert settings.admin_token is None
    assert settings.write_access == "open"


@pytest.mark.parametrize(
    "token", ["short-token", "change-me-" + "x" * 40], ids=["too-short", "placeholder"]
)
def test_production_requires_a_strong_admin_token(token: str) -> None:
    with pytest.raises(ValidationError, match="SITESCOPE_ADMIN_TOKEN must be a real value"):
        Settings(
            _env_file=None,
            environment="production",
            admin_token=SecretStr(token),
            **PRODUCTION_CREDENTIALS,
        )


def test_the_admin_token_is_masked() -> None:
    settings = Settings(_env_file=None, admin_token=SecretStr("never-printed-admin-token-xyz"))

    assert "never-printed-admin-token-xyz" not in repr(settings)
    assert "never-printed-admin-token-xyz" not in str(settings.model_dump())


@pytest.mark.parametrize("value", ["0", "10001"])
def test_rejects_an_invalid_active_jobs_cap(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SITESCOPE_CRAWL_MAX_ACTIVE_JOBS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
