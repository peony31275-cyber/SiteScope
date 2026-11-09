"""The operator commands (Phase 11, ADR-0024) against the real database: they create and
cancel jobs through the services, with every rule of a crawl request, and seed the demo jobs
once. No process runs the jobs here: they stay queued unless cancelled."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import Database
from app.scheduler import admin
from tests.integration import helpers

BASE = "https://docs.example.com"  # approved by crawl_settings


@pytest.fixture
def settings(crawl_settings: Settings) -> Iterator[Settings]:
    yield crawl_settings
    asyncio.run(_truncate(crawl_settings))


async def _truncate(settings: Settings) -> None:
    database = Database(settings, application_name="sitescope-tests")
    try:
        await helpers.truncate_all(database)
    finally:
        await database.dispose()


async def _statuses(settings: Settings) -> list[tuple[str, str]]:
    database = Database(settings, application_name="sitescope-tests")
    try:
        async with database.session() as session:
            rows = await session.execute(
                text("SELECT seed_url, status FROM crawl_jobs ORDER BY created_at, id")
            )
            return [(seed, status) for seed, status in rows]
    finally:
        await database.dispose()


def run(
    settings: Settings, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any]]:
    code = admin.main(list(argv), settings=settings)
    # The command's document is the last one on stdout: the test session's JSON logging may
    # write the services' log lines before it (one line each, never a bare "{").
    lines = capsys.readouterr().out.splitlines()
    start = max(index for index, line in enumerate(lines) if line == "{")
    document: dict[str, Any] = json.loads("\n".join(lines[start:]))
    return code, document


def test_create_follows_the_rules_of_a_crawl_request(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code, created = run(settings, capsys, "create", "--seed", f"{BASE}/a", "--max-depth", "1")
    assert code == 0
    assert created["created"] is True
    assert created["job"]["seed_url"] == f"{BASE}/a"
    assert (created["job"]["max_depth"], created["job"]["max_pages"]) == (1, 100)

    args = ("create", "--seed", f"{BASE}/b", "--idempotency-key", "k-1")
    first, replay = run(settings, capsys, *args)[1], run(settings, capsys, *args)[1]
    assert (first["created"], replay["created"]) == (True, False)
    assert first["job"]["id"] == replay["job"]["id"]

    code, refused = run(settings, capsys, "create", "--seed", "https://not-approved.example/")
    assert code == 1
    assert refused["error"]["code"] == "validation_failed"
    assert refused["error"]["violations"][0]["type"] == "target_not_allowed"
    code, too_deep = run(settings, capsys, "create", "--seed", BASE, "--max-depth", "6")
    assert code == 1
    assert too_deep["error"]["violations"][0]["type"] == "limit_exceeded"

    capped = settings.model_copy(update={"crawl_max_active_jobs": 2})
    code, cap = run(capped, capsys, "create", "--seed", f"{BASE}/c")
    assert code == 1
    assert cap["error"]["code"] == "too_many_active_jobs"
    assert len(asyncio.run(_statuses(settings))) == 2


def test_cancel_cancels_a_queued_job_and_reports_an_unknown_one(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = run(settings, capsys, "create", "--seed", BASE)[1]["job"]["id"]

    code, cancelled = run(settings, capsys, "cancel", job_id)
    assert code == 0
    assert (cancelled["action"], cancelled["status"]) == ("cancelled", "cancelled")
    code, again = run(settings, capsys, "cancel", job_id)
    assert (code, again["action"], again["status"]) == (0, "unchanged", "cancelled")

    code, missing = run(settings, capsys, "cancel", str(uuid.uuid4()))
    assert code == 1
    assert missing["error"]["code"] == "not_found"


def test_seed_demo_creates_the_demo_jobs_once(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code, seeded = run(settings, capsys, "seed-demo", "--base-url", BASE)
    assert code == 0
    assert seeded["created"] == [demo.name for demo in admin.DEMO_JOBS]
    assert seeded["content"].startswith("synthetic")
    assert [(job["seed_url"], job["max_depth"], job["max_pages"]) for job in seeded["jobs"]] == [
        (f"{BASE}/docs/", 2, 50),
        (f"{BASE}/blog/", 1, 20),
        (f"{BASE}/", 3, 6),
        (f"{BASE}/", 3, 100),  # the full crawl last: the newest job
    ]
    assert {job["status"] for job in seeded["jobs"]} == {"queued"}
    assert seeded["settled"] is False  # nothing runs the jobs in this test, and it did not wait

    # Again, with the base URL spelled differently: the same jobs, nothing new.
    code, again = run(settings, capsys, "seed-demo", "--base-url", "HTTPS://Docs.Example.com/")
    assert (code, again["created"]) == (0, [])
    assert [job["id"] for job in again["jobs"]] == [job["id"] for job in seeded["jobs"]]

    # A cancelled demo job no longer stands for its demo: the next run creates it again.
    blog = seeded["jobs"][1]["id"]
    assert run(settings, capsys, "cancel", blog)[0] == 0
    code, recreated = run(settings, capsys, "seed-demo", "--base-url", BASE)
    assert (code, recreated["created"]) == (0, ["blog"])
    assert recreated["jobs"][1]["id"] != blog
    assert len(asyncio.run(_statuses(settings))) == 5


def test_seed_demo_reports_a_wait_that_runs_out(
    settings: Settings, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin, "_POLL_SECONDS", 0.05)

    code, document = run(settings, capsys, "seed-demo", "--base-url", BASE, "--wait", "1")

    assert code == 1
    assert document["error"]["code"] == "wait_timed_out"
    assert document["settled"] is False
    assert len(document["jobs"]) == len(admin.DEMO_JOBS)


def test_an_unapproved_demo_site_is_refused_before_anything_is_created(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = run(settings, capsys, "seed-demo", "--base-url", "https://elsewhere.test")
    assert code == 1
    assert document["error"]["violations"][0]["type"] == "target_not_allowed"
    assert asyncio.run(_statuses(settings)) == []
