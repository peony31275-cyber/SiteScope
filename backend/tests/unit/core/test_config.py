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
