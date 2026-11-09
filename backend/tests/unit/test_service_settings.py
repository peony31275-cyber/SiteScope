"""The settings of the tests that need the services (tests/service_fixtures.py): the
developer's servers, but never the developer's write access or job cap (Phase 11 review)."""

from pydantic import SecretStr

from app.core.config import Settings
from tests.service_fixtures import TEST_REDIS_DB, settings_for_tests


def test_the_tests_ignore_a_developers_admin_token_and_cap() -> None:
    base = Settings(
        _env_file=None,
        postgres_db="sitescope",
        admin_token=SecretStr("t" * 40),
        crawl_max_active_jobs=1,
    )

    tested = settings_for_tests(base)

    assert tested.postgres_db == "sitescope_test"
    assert tested.redis_db == TEST_REDIS_DB
    assert tested.admin_token is None
    assert tested.write_access == "open"
    assert tested.crawl_max_active_jobs == Settings.model_fields["crawl_max_active_jobs"].default
