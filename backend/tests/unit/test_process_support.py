"""The guard of the process tests: child processes never get the development database.

The children read backend/.env, whose database is the development database; the environment
built by ``child_environment`` must always override it with a disposable test database.
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from tests.process_support import child_environment


def settings(**values: object) -> Settings:
    base = {"postgres_db": "sitescope_test", "redis_db": 15}
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]  # test values


def environment(**changes: object) -> dict[str, str]:
    extra = changes.pop("extra", None)
    return child_environment(
        settings(**changes),
        prefix="test-abc",
        hosts=["127.0.0.1"],
        ports=[8080],
        extra=extra,  # type: ignore[arg-type]  # test values
    )


def test_children_get_the_test_database_and_redis_database_15() -> None:
    built = environment()

    assert built["SITESCOPE_POSTGRES_DB"] == "sitescope_test"
    assert built["SITESCOPE_REDIS_DB"] == "15"
    assert built["SITESCOPE_REDIS_KEY_PREFIX"] == "test-abc"
    assert built["SITESCOPE_ENVIRONMENT"] == "test"


def test_the_development_database_is_refused() -> None:
    with pytest.raises(RuntimeError, match="non-test database"):
        environment(postgres_db="sitescope")


def test_another_redis_database_is_refused() -> None:
    with pytest.raises(RuntimeError, match="Redis database 15"):
        environment(redis_db=0)


@pytest.mark.parametrize(
    "extra",
    [
        {"POSTGRES_DB": "sitescope"},
        {"postgres_db": "sitescope"},  # environment names are case-insensitive on Windows
        {"REDIS_DB": "0"},
        {"Redis_Db": "0"},
        {"REDIS_KEY_PREFIX": "sitescope"},
        {"ENVIRONMENT": "production"},
        {"log_level": "DEBUG", "LOG_LEVEL": "INFO"},  # two spellings of one name
    ],
)
def test_extra_settings_cannot_change_where_the_data_lives(extra: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="extra settings"):
        environment(extra=extra)


def test_inherited_sitescope_variables_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITESCOPE_POSTGRES_DB", "sitescope")
    monkeypatch.setenv("sitescope_redis_db", "0")

    built = environment(extra={"LOG_LEVEL": "DEBUG"})

    assert built["SITESCOPE_POSTGRES_DB"] == "sitescope_test"
    assert built["SITESCOPE_REDIS_DB"] == "15"
    assert built["SITESCOPE_LOG_LEVEL"] == "DEBUG"
    assert [name for name in built if name.upper() == "SITESCOPE_POSTGRES_DB"] == [
        "SITESCOPE_POSTGRES_DB"
    ]


def test_a_developers_admin_token_and_cap_never_reach_the_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend/.env may hold an admin token or a low cap, to try a deployment's modes
    (docs/development.md); the children read that file too, so their environment must
    override both, or the process tests' writes would be refused (Phase 11 review)."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SITESCOPE_ADMIN_TOKEN=" + "t" * 40 + "\nSITESCOPE_CRAWL_MAX_ACTIVE_JOBS=1\n",
        encoding="utf-8",
    )
    built = environment(admin_token=SecretStr("t" * 40), crawl_max_active_jobs=1)

    # The child: exactly that environment, and the same backend/.env.
    for name in list(os.environ):
        if name.upper().startswith("SITESCOPE_"):
            monkeypatch.delenv(name)
    for name, value in built.items():
        if name.startswith("SITESCOPE_"):
            monkeypatch.setenv(name, value)
    child = Settings(_env_file=dotenv)

    assert child.admin_token is None
    assert child.write_access == "open"
    assert child.crawl_max_active_jobs == Settings.model_fields["crawl_max_active_jobs"].default
