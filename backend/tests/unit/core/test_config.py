from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.log_level == "INFO"


def test_reads_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_ENVIRONMENT", "production")
    monkeypatch.setenv("SITESCOPE_LOG_LEVEL", "WARNING")

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
