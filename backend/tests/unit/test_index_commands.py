"""The index dispatcher's configuration and the search command's output, without services."""

import json
import uuid
from datetime import UTC, datetime

from app.api.v1.crawl_schemas import CrawlJobDetail
from app.core.config import Settings
from app.db.models import CrawlJob, UrlState
from app.indexing.queries import Snippet
from app.scheduler.index_dispatcher import IndexDispatcherConfig, IndexPassReport
from app.scheduler.search import render
from app.services.crawl_jobs import CrawlJobProgress
from app.services.search import SearchHit, SearchResults
from app.services.search_index import IndexingProgress

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_index_dispatcher_config_from_settings() -> None:
    settings = Settings(_env_file=None)

    config = IndexDispatcherConfig.from_settings(settings)

    assert (config.dispatch_batch, config.max_outstanding) == (200, 1000)
    assert config.interval == 0.5
    assert config.reconcile_after == 300
    assert config.reconcile_interval == 30  # reconcile_after / 4, at most 30 s
    assert config.maintenance_interval == 60  # retention / 4, at most 60 s
    assert config.name.startswith("index-dispatcher-")
    short = IndexDispatcherConfig(reconcile_after=10, retention_seconds=600)
    assert (short.reconcile_interval, short.maintenance_interval) == (2.5, 60)


def test_an_index_pass_is_busy_only_when_it_changed_something() -> None:
    assert not IndexPassReport(queued=7).busy
    assert IndexPassReport(dispatched=1).busy
    assert IndexPassReport(republished=1).busy
    assert IndexPassReport(completed_jobs=1).busy
    assert IndexPassReport(failed_jobs=1).busy
    # A failed job is worth a log line, but it is not progress (Phase 10 review).
    assert IndexPassReport(completed_jobs=1).progressed
    assert not IndexPassReport(failed_jobs=1).progressed


def test_the_search_command_prints_ascii_json_with_highlights_and_freshness() -> None:
    page_id, job_id = uuid.uuid4(), uuid.uuid4()
    results = SearchResults(
        job_id=job_id,
        query="redis",
        searchable=True,
        total=1,
        limit=10,
        offset=0,
        hits=[
            SearchHit(
                page_id=page_id,
                url="https://docs.example.com/redis",
                title="Redis \x1b[31mstreams\u202e",
                rank=0.6079271,
                snippet=Snippet("Redis streams", ((0, 5),)),
                fetched_at=NOW,
                indexed_at=NOW,
                content_version=1,
            )
        ],
        indexing=IndexingProgress(indexed=1, completed_at=NOW),
    )

    document = render(results)
    printed = json.dumps(document, indent=2)

    assert document["results"][0]["highlights"] == [[0, 5]]
    assert document["results"][0]["rank"] == 0.607927
    assert document["indexing"] == {
        "status": "indexed",
        "pending": 0,
        "queued": 0,
        "indexed": 1,
        "failed": 0,
        "active": 0,
        "completed_at": NOW.isoformat(),
    }
    assert printed.isascii()  # page text never reaches the terminal unescaped
    assert "\x1b" not in printed


def test_the_job_detail_reports_indexing_progress() -> None:
    job = CrawlJob(
        id=uuid.uuid4(),
        status="completed",
        seed_url="https://docs.example.com/",
        allowed_hosts=["docs.example.com"],
        max_depth=1,
        max_pages=10,
        current_level=1,
        admitted_count=3,
        failure_reason=None,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        cancel_requested_at=None,
        indexing_completed_at=None,
    )
    progress = CrawlJobProgress(
        job=job,
        urls_by_state=dict.fromkeys(UrlState, 0) | {UrlState.FETCHED: 3},
        indexing=IndexingProgress(pending=1, queued=1, indexed=1, active=1),
    )

    detail = CrawlJobDetail.from_progress(progress).model_dump(mode="json")

    assert detail["indexing"] == {
        "status": "running",
        "documents_total": 3,
        "documents_by_status": {"pending": 1, "queued": 1, "indexed": 1, "failed": 0},
        "active": 1,
        "completed_at": None,
    }
