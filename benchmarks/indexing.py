r"""Indexing throughput and latency with 1, 2 and 4 indexer processes, and search latency (Phase 7).

What it measures:

1. **Indexing.** One index dispatcher process and N indexer processes (separate operating-system
   processes, each running ``--concurrency`` tasks) index a generated dataset of ``--pages`` pages
   of one job. The clock starts at the commit that stores the pages (their search documents are
   ``pending``), read from PostgreSQL, and every document's latency is its ``indexed_at`` minus
   that commit time, also by the PostgreSQL clock. Throughput is pages divided by the time from
   the commit to the last ``indexed_at``. Process start-up is excluded (the processes are running
   before the commit). Each configuration runs ``--repeat`` times; every run is reported.
2. **Search.** On the indexed dataset of the last run, after ``VACUUM (ANALYZE)`` (which also
   merges the GIN index's pending list), a fixed set of queries (a common term, a rare one, two
   terms, an OR, a phrase, no match, stop words only) runs ``--queries`` times each through the
   search service, one call at a time; every call's wall time is kept.
3. **Query plans.** ``EXPLAIN (ANALYZE, BUFFERS)`` of the search statement for a common and a
   rare term, saved as text.

The dataset is generated from a seeded random generator (the same for every run): pseudo-words
from a 5,000-word vocabulary with a Zipf-like frequency, a 5-word title, a 15-word description,
20 words of headings and ``--words`` words of body per page.

Isolation: a disposable database ``<SITESCOPE_POSTGRES_DB>_test_bench`` (created, migrated and
dropped by this script), Redis database 15 under a unique key prefix per run (deleted at the end).
Nothing leaves the machine. The numbers describe this machine only.

Run from backend\ (PostgreSQL and Redis up, as for the tests):

    .\.venv\Scripts\python.exe ..\benchmarks\indexing.py `
        --output ..\benchmarks\results\phase7-indexing.json `
        --plans ..\benchmarks\results\phase7-search-plans.txt
"""

import argparse
import asyncio
import json
import platform
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402 - after the path setup

from app.core.config import Settings  # noqa: E402
from app.core.redis import create_redis_client  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.indexing.documents import TEXT_SEARCH_CONFIG  # noqa: E402
from app.indexing.queries import HEADLINE_OPTIONS, SNIPPET_SOURCE_CHARS  # noqa: E402
from app.services import search as search_service  # noqa: E402
from app.services.search import search_job  # noqa: E402
from tests.crawl_support import new_job  # noqa: E402
from tests.integration import helpers  # noqa: E402
from tests.process_support import Cluster, child_environment  # noqa: E402

HOST = "bench.test"
SEED = 7

STORE_PAGES = text(
    """
    WITH data AS (
        SELECT * FROM unnest(CAST(:paths AS text[]), CAST(:titles AS text[]),
                             CAST(:descriptions AS text[]), CAST(:headings AS text[]),
                             CAST(:bodies AS text[]))
               AS t(path, title, description, headings, body)
    ),
    urls AS (
        INSERT INTO crawl_urls (job_id, normalized_url, depth, state, result_epoch, outcome,
                                http_status, finished_at)
        SELECT :job_id, 'https://bench.test/' || path, 1, 'fetched', 0, 'html_stored', 200,
               clock_timestamp()
        FROM data
        RETURNING id, normalized_url
    ),
    stored AS (
        INSERT INTO pages (job_id, crawl_url_id, normalized_url, http_status, content_type,
                           title, meta_description, headings, extracted_text, content_hash,
                           byte_size, fetched_at)
        SELECT :job_id, urls.id, urls.normalized_url, 200, 'text/html', data.title,
               data.description, data.headings, data.body, sha256(convert_to(data.body, 'UTF8')),
               octet_length(data.body), clock_timestamp()
        FROM urls JOIN data ON urls.normalized_url = 'https://bench.test/' || data.path
        RETURNING id
    )
    INSERT INTO search_documents (page_id, job_id) SELECT id, :job_id FROM stored
    """
)

LATENCIES = text(
    """
    SELECT count(*) FILTER (WHERE status = 'indexed') AS indexed,
           extract(epoch FROM max(indexed_at) - CAST(:committed AS timestamptz)) AS span,
           percentile_cont(ARRAY[0.5, 0.95, 1.0]) WITHIN GROUP (
               ORDER BY extract(epoch FROM indexed_at - CAST(:committed AS timestamptz))
           ) AS quantiles,
           max(attempt_count) AS max_attempts
    FROM search_documents WHERE job_id = :job_id
    """
)


def vocabulary(size: int, rng: random.Random) -> list[str]:
    consonants, vowels = "bdfgklmnprstvz", "aeiou"
    words: set[str] = set()
    while len(words) < size:
        syllables = rng.randint(2, 4)
        words.add("".join(rng.choice(consonants) + rng.choice(vowels) for _ in range(syllables)))
    return sorted(words)  # sorted, then ranked by the generator below: deterministic


def dataset(pages: int, words: int) -> tuple[list[str], dict[str, list[str]]]:
    rng = random.Random(SEED)  # noqa: S311 - a reproducible dataset, not a secret
    vocab = vocabulary(5000, rng)
    weights = [1 / (rank + 1) ** 1.1 for rank in range(len(vocab))]

    def phrase(count: int) -> str:
        return " ".join(rng.choices(vocab, weights=weights, k=count))

    columns: dict[str, list[str]] = {
        "paths": [],
        "titles": [],
        "descriptions": [],
        "headings": [],
        "bodies": [],
    }
    for index in range(pages):
        columns["paths"].append(f"page-{index:06d}")
        columns["titles"].append(phrase(5))
        columns["descriptions"].append(phrase(15))
        columns["headings"].append(phrase(20))
        columns["bodies"].append(phrase(words))
    return vocab, columns


async def store(database: Database, job_id: uuid.UUID, columns: dict[str, list[str]]) -> datetime:
    """Insert every page in one transaction; returns the commit time by the database clock."""
    async with database.transaction() as session:
        await session.execute(STORE_PAGES, {"job_id": job_id, **columns})
    async with database.session() as session:
        committed: datetime = await session.scalar(text("SELECT clock_timestamp()"))
    return committed


async def one_run(
    settings: Settings,
    database: Database,
    columns: dict[str, list[str]],
    *,
    indexers: int,
    concurrency: int,
    log_dir: Path,
) -> tuple[dict[str, Any], uuid.UUID]:
    await helpers.truncate_all(database)
    pages = len(columns["paths"])
    job_id, _ = await new_job(database, host=HOST)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    environment = child_environment(
        settings,
        prefix=prefix,
        hosts=["127.0.0.1"],
        ports=[80],
        extra={
            "LOG_LEVEL": "INFO",
            "INDEX_CONCURRENCY": str(concurrency),
            "INDEX_DISPATCH_BATCH_SIZE": "1000",
            "INDEX_MAX_OUTSTANDING": str(max(1000, pages)),
            "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.1",
        },
    )
    run_dir = log_dir / f"i{indexers}-{prefix}"
    run_dir.mkdir(parents=True)
    cluster = Cluster(environment, run_dir)
    try:
        processes = [cluster.index_dispatcher()] + [cluster.indexer() for _ in range(indexers)]
        await cluster.started(*processes)
        committed = await store(database, job_id, columns)
        started = time.perf_counter()
        while True:
            async with database.session() as session:
                row = (
                    await session.execute(LATENCIES, {"job_id": job_id, "committed": committed})
                ).one()
            if row.indexed == pages:
                break
            if time.perf_counter() - started > 900:
                raise RuntimeError("a run took more than 15 minutes")
            await asyncio.sleep(0.1)
        exit_codes = await cluster.stop_all()
    finally:
        cluster.kill_all()
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
    per_indexer = {
        process.name: sum(
            1 for line in process.events("task.handled") if line["reason"] == "indexed"
        )
        for process in cluster.indexers()
    }
    reasons = Counter(
        line["reason"] for process in cluster.indexers() for line in process.events("task.handled")
    )
    p50, p95, maximum = (round(float(value), 3) for value in row.quantiles)
    span = float(row.span)
    return (
        {
            "indexers": indexers,
            "concurrency_per_indexer": concurrency,
            "pages": pages,
            "body_words": len(columns["bodies"][0].split()),
            "seconds_commit_to_last_indexed": round(span, 3),
            "pages_per_second": round(pages / span, 1),
            "latency_seconds_p50": p50,
            "latency_seconds_p95": p95,
            "latency_seconds_max": maximum,
            "max_attempt_count": row.max_attempts,
            "indexed_per_indexer": per_indexer,
            "handled_by_reason": dict(reasons),
            "exit_codes": exit_codes,
        },
        job_id,
    )


def queries(vocab: list[str], columns: dict[str, list[str]]) -> dict[str, str]:
    # The vocabulary is ranked by the Zipf weights in generation order: index 0 is the most
    # frequent word, larger indexes are rarer.
    first_words = columns["bodies"][0].split()
    return {
        "common_term": vocab[0],
        "medium_term": vocab[100],
        "rare_term": vocab[3000],
        "two_terms": f"{vocab[0]} {vocab[100]}",
        "or_terms": f"{vocab[100]} or {vocab[3000]}",
        "phrase": f'"{first_words[10]} {first_words[11]}"',
        "no_match": "zzzqqqxxx",
        "stop_words_only": "the and of",
    }


async def search_latency(
    database: Database, job_id: uuid.UUID, cases: dict[str, str], repeat: int
) -> list[dict[str, Any]]:
    results = []
    for name, query in cases.items():
        timings = []
        total = 0
        searchable = True
        for _ in range(repeat):
            async with database.session() as session:
                begun = time.perf_counter()
                found = await search_job(session, job_id, query, limit=10)
                timings.append((time.perf_counter() - begun) * 1000)
            total, searchable = found.total, found.searchable
        timings.sort()
        results.append(
            {
                "query_kind": name,
                "query": query,
                "searchable": searchable,
                "matches": total,
                "runs": repeat,
                "ms_min": round(timings[0], 2),
                "ms_p50": round(statistics.median(timings), 2),
                "ms_p95": round(timings[max(0, round(0.95 * len(timings)) - 1)], 2),
                "ms_max": round(timings[-1], 2),
            }
        )
    return results


async def plans(database: Database, job_id: uuid.UUID, cases: dict[str, str]) -> str:
    sql = "EXPLAIN (ANALYZE, BUFFERS) " + str(search_service._SEARCH.text)
    output = []
    for name in ("common_term", "rare_term"):
        params = {
            "config": TEXT_SEARCH_CONFIG,
            "query": cases[name],
            "job_id": job_id,
            "limit": 10,
            "offset": 0,
            "snippet_chars": SNIPPET_SOURCE_CHARS,
            "headline_options": HEADLINE_OPTIONS,
        }
        async with database.session() as session:
            rows = await session.execute(text(sql), params)
            lines = [row[0] for row in rows]
        output.append(f"--- {name}: {cases[name]!r} ---\n" + "\n".join(lines) + "\n")
    return "\n".join(output)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--indexers", default="1,2,4", help="indexer counts (comma-separated)")
    parser.add_argument("--concurrency", type=int, default=4, help="tasks per indexer process")
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--words", type=int, default=1000, help="body words per page")
    parser.add_argument("--repeat", type=int, default=3, help="runs per configuration")
    parser.add_argument("--queries", type=int, default=30, help="calls per search query")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plans", type=Path, required=True)
    parser.add_argument("--logs", type=Path, default=Path("benchmark-logs"))
    args = parser.parse_args()

    base = Settings()
    settings = base.model_copy(
        update={"postgres_db": f"{base.postgres_db}_test_bench", "redis_db": 15}
    )
    helpers.require_disposable(settings.postgres_db)
    vocab, columns = dataset(args.pages, args.words)
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, "head")
    database = Database(settings, application_name="sitescope-benchmark")
    runs: list[dict[str, Any]] = []
    try:
        job_id = None
        for indexers in (int(value) for value in args.indexers.split(",")):
            for repeat in range(args.repeat):
                result, job_id = await one_run(
                    settings,
                    database,
                    columns,
                    indexers=indexers,
                    concurrency=args.concurrency,
                    log_dir=args.logs,
                )
                result["repeat"] = repeat + 1
                runs.append(result)
                print(json.dumps(result), flush=True)  # noqa: T201 - the benchmark's output
        if job_id is None:
            raise RuntimeError("no indexing run")
        # A settled index: VACUUM merges the GIN index's pending list (fast-update entries of
        # the bulk indexing), which the planner otherwise counts against an index scan.
        async with database.engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text("VACUUM (ANALYZE) pages, search_documents"))
        cases = queries(vocab, columns)
        searches = await search_latency(database, job_id, cases, args.queries)
        for line in searches:
            print(json.dumps(line), flush=True)  # noqa: T201
        plan_text = await plans(database, job_id, cases)
        async with database.session() as session:
            sizes = (
                await session.execute(
                    text(
                        "SELECT pg_size_pretty(pg_relation_size('search_documents')) AS heap,"
                        " pg_size_pretty(pg_total_relation_size('search_documents')) AS total,"
                        " pg_size_pretty(pg_relation_size('ix_search_documents_search_vector'))"
                        " AS gin, (SELECT version()) AS version"
                    )
                )
            ).one()
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "postgresql": sizes.version,
        "command": " ".join(sys.argv),
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    dataset_info = {
        "pages": args.pages,
        "body_words": args.words,
        "seed": SEED,
        "search_documents_heap": sizes.heap,
        "search_documents_total": sizes.total,
        "gin_index": sizes.gin,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "environment": environment,
                "dataset": dataset_info,
                "indexing_runs": runs,
                "search": searches,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.plans.write_text(plan_text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
