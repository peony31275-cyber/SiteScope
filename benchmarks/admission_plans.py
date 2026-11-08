r"""The level admission's plan with stale and with fresh statistics (Phase 10).

Reproduces the slow BFS level transitions found by ``crawl_scaling.py``: the scheduler's
admission statement (``crawl_state._ADMIT_NEXT_LEVEL``) is planned from PostgreSQL's statistics,
and for a job those statistics have not seen yet (they describe earlier jobs), every row count
of the new job is estimated at one row. The plan then chains nested loops whose inner sides scan
the whole job, with the join conditions as filters.

**Setup** (a disposable database; nothing else runs): an earlier job built and analyzed with the
Phase 8 generator (``analysis.py``), then a new running job whose BFS levels 0 to ``--level`` are
stored (pages and links from the same generator) without analyzing again, as a crawl in progress
leaves them before autovacuum runs. Autovacuum is off on the three tables from then on, so the
statistics change only when the benchmark analyzes; the output records the planner's row counts
and the tables' analyze counters before and after each arm.

**Measured**, each ``--repeat`` times inside a rolled-back transaction: ``EXPLAIN (ANALYZE)`` of
the admission of level ``--level + 1``, with the statistics as they are (stale) and after
``ANALYZE`` (fresh), each with nested loops allowed and with ``enable_nestloop = off`` (what the
scheduler sets for this statement since Phase 10). Then, with the same statistics, the job's other
per-job reads, as the API calls them: the job detail and every website-analysis use case of
``analysis.py`` (a call stopped by the analysis time limit is recorded as such).

Run from backend\ with PostgreSQL up::

    .\.venv\Scripts\python.exe ..\benchmarks\admission_plans.py `
        --output ..\benchmarks\results\phase10-admission-plans.json `
        --plans ..\benchmarks\results\phase10-admission-plans.txt
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from analysis import INSERT_LINKS, INSERT_PAGES, INSERT_URLS, dataset, store
from analysis import cases as analysis_cases
from benchlib import bench_settings, emit, environment, fresh_database, write_json
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.db.session import Database
from app.services.crawl_jobs import get_crawl_job
from app.services.crawl_state import _ADMIT_NEXT_LEVEL
from app.services.site_analysis import AnalysisTimeoutError
from tests.crawl_support import new_job
from tests.integration import helpers


async def store_partial(database: Database, pages: int, links: int, level: int) -> uuid.UUID:
    """A running job whose levels 0..``level`` are stored, not analyzed."""
    job_id, _ = await new_job(database, max_depth=10, max_pages=100_000, host="bench.test")
    urls, link_columns = dataset(pages, links)
    keep = [index for index, depth in enumerate(urls["depths"]) if depth <= level]
    urls = {key: [values[index] for index in keep] for key, values in urls.items()}
    async with database.transaction() as session:
        await session.execute(
            text(
                "UPDATE crawl_urls SET state = 'fetched', outcome = 'html_stored',"
                " http_status = 200, result_epoch = 0, finished_at = clock_timestamp()"
                " WHERE job_id = :job_id"
            ),
            {"job_id": job_id},
        )
        await session.execute(INSERT_URLS, {"job_id": job_id, **urls})
        await session.execute(INSERT_PAGES, {"job_id": job_id})
        await session.execute(
            text(
                "UPDATE crawl_jobs SET current_level = :level,"
                " admitted_count = (SELECT count(*) FROM crawl_urls WHERE job_id = :job_id)"
                " WHERE id = :job_id"
            ),
            {"job_id": job_id, "level": level},
        )
    # Links of the stored pages only (INSERT_LINKS joins on them).
    batch = 50_000
    for start in range(0, len(link_columns["sources"]), batch):
        chunk = {key: values[start : start + batch] for key, values in link_columns.items()}
        async with database.transaction() as session:
            await session.execute(INSERT_LINKS, {"job_id": job_id, **chunk})
    return job_id


STATEMENT_TIMEOUT_MS = 60_000


async def admission(
    database: Database, job_id: uuid.UUID, level: int, *, nestloop: bool
) -> tuple[float | None, str, int]:
    """EXPLAIN ANALYZE of the admission, rolled back: (execution ms, plan text, rows); the time
    is None when the statement ran into its 60 s timeout."""
    try:
        return await _admission(database, job_id, level, nestloop=nestloop)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "57014":  # query_canceled
            raise
        return None, f"(stopped by statement_timeout after {STATEMENT_TIMEOUT_MS} ms)", -1


async def _admission(
    database: Database, job_id: uuid.UUID, level: int, *, nestloop: bool
) -> tuple[float | None, str, int]:
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            if not nestloop:
                await connection.execute(text("SET LOCAL enable_nestloop = off"))
            remaining = await connection.scalar(
                text("SELECT max_pages - admitted_count FROM crawl_jobs WHERE id = :id"),
                {"id": job_id},
            )
            begun = time.perf_counter()
            rows = await connection.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + str(_ADMIT_NEXT_LEVEL.text)),
                {
                    "job_id": job_id,
                    "level": level,
                    "next_level": level + 1,
                    "remaining": remaining,
                },
            )
            lines = [row[0] for row in rows]
            elapsed = (time.perf_counter() - begun) * 1000
        finally:
            await transaction.rollback()
    admitted = next(
        (
            # PostgreSQL 18 prints actual rows with decimals ("rows=977.00").
            round(float(line.split("actual time=")[1].split("rows=")[1].split()[0]))
            for line in lines
            if line.lstrip().startswith("Insert on crawl_urls") and "rows=" in line
        ),
        -1,
    )
    return elapsed, "\n".join(lines), admitted


async def reads(database: Database, job_id: uuid.UUID, repeat: int) -> dict[str, Any]:
    """The job detail and every analysis use case of the job: milliseconds per call (a new
    session each, as the API uses), or the error that stopped it (the analysis time limit)."""
    calls: dict[str, Any] = {"job detail": lambda session: get_crawl_job(session, job_id)}
    calls.update(await analysis_cases(database, job_id))
    measured: dict[str, Any] = {}
    for name, call in calls.items():
        timings: list[float] = []
        error = None
        for _ in range(repeat):
            async with database.session() as session:
                begun = time.perf_counter()
                try:
                    await call(session)
                except Exception as exc:  # recorded: a timeout is a result here
                    error = type(exc).__name__
                    break
                timings.append(round((time.perf_counter() - begun) * 1000, 1))
        measured[name] = {
            "ms": timings,
            "ms_median": statistics.median(timings) if timings else None,
            "error": error,
        }
    return measured


async def captured(database: Database, call: Any) -> list[tuple[str, Any]]:
    """The statements a service call executes (SQL text and driver parameters), in order; a
    call stopped by its time limit keeps the statements it got to, the stopped one last."""
    seen: list[tuple[str, Any]] = []

    def record(*args: Any) -> None:  # (connection, cursor, statement, parameters, ...)
        seen.append((str(args[2]), args[3]))

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with database.session() as session:
            await call(session)
    except AnalysisTimeoutError:
        pass  # recorded by reads(); the statements are explained anyway
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    return seen


def _is_setting(sql: str) -> bool:
    head = " ".join(sql.split())[:40].upper()
    return head.startswith("SET ") or head.startswith("SELECT SET_CONFIG")


def _nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    found = [node]
    for child in node.get("Plans", []):
        found += _nodes(child)
    return found


async def explain_reads(
    database: Database, job_id: uuid.UUID, label: str
) -> tuple[dict[str, Any], list[str]]:
    """``EXPLAIN (ANALYZE)`` of every statement each read executes, in one rolled-back
    transaction per read that replays the read's own settings first (statement timeout, plan
    cache mode), with nested loops allowed and then with them off, whatever the service sets:
    the plans of both are measured with the same statistics. Per statement: milliseconds, or the
    error that stopped it (its time limit)."""
    calls: dict[str, Any] = {"job detail": lambda session: get_crawl_job(session, job_id)}
    calls.update(await analysis_cases(database, job_id))
    summary: dict[str, Any] = {}
    rendered: list[str] = []
    for name, call in calls.items():
        statements = await captured(database, call)
        entries = []
        for variant in ("nested loops allowed", "nested loops off"):
            async with database.engine.connect() as connection:
                raw = await connection.get_raw_connection()
                driver = raw.driver_connection
                if driver is None:
                    raise RuntimeError("no driver connection")
                transaction = driver.transaction()
                await transaction.start()
                try:
                    for index, (sql, parameters) in enumerate(statements):
                        if _is_setting(sql):
                            # The isolation level does not change a plan, and SET TRANSACTION
                            # must come first; the other settings are replayed in order.
                            if not " ".join(sql.split()).upper().startswith("SET TRANSACTION"):
                                await driver.execute(sql, *parameters)
                            continue
                        allowed = "on" if variant == "nested loops allowed" else "off"
                        await driver.execute(f"SET LOCAL enable_nestloop = {allowed}")
                        entry: dict[str, Any] = {
                            "variant": variant,
                            "statement": index,
                            "sql": " ".join(sql.split())[:80],
                        }
                        try:
                            await driver.execute("SAVEPOINT explained")
                            plan = await driver.fetchval(
                                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, *parameters
                            )
                            root = (plan if isinstance(plan, list) else json.loads(plan))[0]
                            text_plan = None
                            if root["Execution Time"] >= 100:  # the slow ones, in full
                                text_plan = await driver.fetch(
                                    "EXPLAIN (ANALYZE) " + sql, *parameters
                                )
                            await driver.execute("RELEASE SAVEPOINT explained")
                        except Exception as exc:  # a statement stopped by its time limit
                            await driver.execute("ROLLBACK TO SAVEPOINT explained")
                            entry["error"] = type(exc).__name__
                            entries.append(entry)
                            continue
                        entry["execution_ms"] = round(root["Execution Time"], 1)
                        entry["nested_loops"] = sum(
                            1 for node in _nodes(root["Plan"]) if node["Node Type"] == "Nested Loop"
                        )
                        entries.append(entry)
                        if text_plan is not None:
                            rendered.append(
                                f"--- {label}, read: {name}, statement {index} ({variant}),"
                                f" {entry['execution_ms']} ms ---\n"
                                + "\n".join(row[0] for row in text_plan)
                                + "\n"
                            )
                finally:
                    await transaction.rollback()
        summary[name] = entries
    return summary, rendered


_TABLES = ("crawl_urls", "pages", "page_links")
_STATISTICS_STATE = text(
    """
    SELECT c.relname, CAST(c.reltuples AS bigint) AS reltuples, s.n_live_tup,
           s.n_mod_since_analyze, s.analyze_count, s.autoanalyze_count
    FROM pg_class AS c JOIN pg_stat_user_tables AS s ON s.relid = c.oid
    WHERE c.relname IN ('crawl_urls', 'pages', 'page_links')
    ORDER BY c.relname
    """
)


async def set_autovacuum(database: Database, *, enabled: bool) -> None:
    value = "on" if enabled else "off"
    async with database.transaction() as session:
        for table in _TABLES:
            await session.execute(text(f"ALTER TABLE {table} SET (autovacuum_enabled = {value})"))


async def statistics_state(database: Database) -> dict[str, dict[str, int]]:
    async with database.session() as session:
        rows = await session.execute(_STATISTICS_STATE)
        return {
            row.relname: {
                "reltuples": int(row.reltuples),
                "n_live_tup": int(row.n_live_tup),
                "n_mod_since_analyze": int(row.n_mod_since_analyze),
                "analyze_count": int(row.analyze_count),
                "autoanalyze_count": int(row.autoanalyze_count),
            }
            for row in rows
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--earlier-pages", type=int, default=2000, help="the analyzed job")
    parser.add_argument("--pages", type=int, default=5000, help="the new job's generator size")
    parser.add_argument("--links", type=int, default=12, help="links per page")
    parser.add_argument("--level", type=int, default=2, help="the new job's last stored level")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plans", type=Path, required=True)
    args = parser.parse_args()
    # The driver's own timeout must outlast the statement timeout, which PostgreSQL enforces.
    settings = bench_settings().model_copy(update={"db_command_timeout_seconds": 120.0})
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    results: list[dict[str, Any]] = []
    read_results: dict[str, Any] = {}
    read_plans: dict[str, Any] = {}
    plan_text: list[str] = []
    statistics_seen: dict[str, Any] = {}
    try:
        env_info = await environment(settings, database)
        await store(database, args.earlier_pages, args.links)  # analyzed (VACUUM ANALYZE)
        # From here the statistics change only when this benchmark analyzes: an autovacuum
        # run during the stale arm (a timed-out minute is long enough) would refresh them.
        await set_autovacuum(database, enabled=False)
        job_id = await store_partial(database, args.pages, args.links, args.level)
        for stats in ("stale", "fresh"):
            if stats == "fresh":
                async with database.engine.connect() as connection:
                    autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
                    await autocommit.execute(text("ANALYZE crawl_urls, pages, page_links"))
            statistics_seen[f"{stats}_before"] = await statistics_state(database)
            for nestloop in (True, False):
                timings = []
                rendered = ""
                admitted = -1
                timed_out = 0
                for _ in range(args.repeat):
                    elapsed, rendered, admitted = await admission(
                        database, job_id, args.level, nestloop=nestloop
                    )
                    if elapsed is None:
                        timed_out += 1
                        break  # each further try would take the same minute
                    timings.append(round(elapsed, 1))
                record = {
                    "statistics": stats,
                    "nested_loops": "allowed" if nestloop else "off",
                    "ms": timings,
                    "ms_median": statistics.median(timings) if timings else None,
                    "timed_out_after_ms": STATEMENT_TIMEOUT_MS if timed_out else None,
                    "admitted": admitted,
                }
                results.append(record)
                emit(record)
                label = f"statistics {stats}, nested loops {record['nested_loops']}"
                plan_text.append(f"--- {label} ---\n{rendered}\n")
            # The job's other per-job reads, planned from the same statistics.
            read_results[stats] = await reads(database, job_id, args.repeat)
            for name, measured in read_results[stats].items():
                emit({"statistics": stats, "read": name, **measured})
            read_plans[stats], rendered_reads = await explain_reads(
                database, job_id, f"statistics {stats}"
            )
            plan_text.extend(rendered_reads)
            for name, entries in read_plans[stats].items():
                slowest = {
                    variant: max(
                        (
                            entry["execution_ms"]
                            for entry in entries
                            if entry["variant"] == variant and "execution_ms" in entry
                        ),
                        default=None,
                    )
                    for variant in ("nested loops allowed", "nested loops off")
                }
                errors = sorted({entry["error"] for entry in entries if "error" in entry})
                emit(
                    {
                        "statistics": stats,
                        "read plans": name,
                        "slowest_statement_ms": slowest,
                        "errors": errors,
                    }
                )
            statistics_seen[f"{stats}_after"] = await statistics_state(database)
            emit({"statistics": stats, "tables": statistics_seen[f"{stats}_after"]})
        async with database.session() as session:
            sizes = (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM crawl_urls WHERE job_id = :id) AS urls,"
                        " (SELECT count(*) FROM page_links WHERE job_id = :id) AS links,"
                        " (SELECT count(*) FROM crawl_urls WHERE job_id = :id AND depth = :level)"
                        " AS level_urls"
                    ),
                    {"id": job_id, "level": args.level},
                )
            ).one()
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    write_json(
        args.output,
        {
            "benchmark": "admission_plans",
            "environment": env_info,
            "configuration": {
                "earlier_job_pages": args.earlier_pages,
                "new_job_generator_pages": args.pages,
                "links_per_page": args.links,
                "admitted_level": args.level + 1,
                "new_job_urls": sizes.urls,
                "new_job_links": sizes.links,
                "level_urls": sizes.level_urls,
                "repeat": args.repeat,
                "autovacuum": "off on crawl_urls, pages and page_links after the earlier job",
            },
            "results": results,
            "reads": read_results,
            # EXPLAIN (ANALYZE) of every statement of every read: as the service plans it, and
            # with nested loops off (explain_reads).
            "read_plans": read_plans,
            # The planner's row counts (reltuples) and the tables' analyze counters before and
            # after each arm: they show what the planner knew.
            "statistics_state": statistics_seen,
        },
    )
    args.plans.write_text("\n".join(plan_text), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
