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
