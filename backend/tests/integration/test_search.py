"""The search service (app/services/search.py) against PostgreSQL: relevance, order, paging,
the visibility rule, job isolation, snippets, and awkward queries. No machine learning: ranking
is PostgreSQL's ``ts_rank`` over weighted vectors, and every expectation below follows from it.
"""

import time
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.errors import InvalidInputError, NotFoundError
from app.db.session import Database
from app.indexing.queries import MAX_OFFSET, MAX_QUERY_CHARS
from app.queue.streams import IndexStream
from app.services import search as search_module
from app.services.search import SearchResults, SearchTimeoutError, search_job
from app.services.search_index import IndexingProgress, read_job_indexing, request_reindex
from tests.crawl_support import new_job
from tests.index_support import (
    add_page,
    change_content,
    execute,
    finish_job,
    index_everything,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> IndexStream:
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


async def search(
    database: Database, job_id: uuid.UUID, query: str, *, limit: int = 10, offset: int = 0
) -> SearchResults:
    async with database.session() as session:
        return await search_job(session, job_id, query, limit=limit, offset=offset)


def urls(results: SearchResults) -> list[str]:
    return [hit.url.removeprefix("https://example.test/") for hit in results.hits]


# --- Relevance -----------------------------------------------------------------------------------


async def test_a_title_match_ranks_above_a_comparable_body_match(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="body", title="Notes", body="Redis streams guide")
    await add_page(database, job_id, path="title", title="Redis", body="Notes streams guide")
    await add_page(database, job_id, path="heading", title="Notes", headings="Redis", body="x")
    await add_page(database, job_id, path="description", title="N", description="Redis", body="")
    await index_everything(database, stream)

    results = await search(database, job_id, "redis")

    assert urls(results) == ["title", "description", "heading", "body"]
    ranks = [hit.rank for hit in results.hits]
    assert ranks == sorted(ranks, reverse=True)
    assert ranks[0] > ranks[1] > ranks[2] > ranks[3] > 0


async def test_repeating_a_word_in_the_body_never_outranks_one_title_match(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="title", title="Kafka", body="")
    await add_page(database, job_id, path="spam", title="Spam", body="kafka " * 500)
    await index_everything(database, stream)

    assert urls(await search(database, job_id, "kafka")) == ["title", "spam"]


async def test_more_matching_terms_rank_higher_and_unrelated_pages_are_excluded(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="both", body="python asyncio event loop")
    await add_page(database, job_id, path="python", body="python packaging")
    await add_page(database, job_id, path="asyncio", body="asyncio tasks")
    await add_page(database, job_id, path="unrelated", body="gardening tips")
    await index_everything(database, stream)

    either = await search(database, job_id, "python or asyncio")
    both = await search(database, job_id, "python asyncio")  # all words are required

    assert urls(either)[0] == "both"
    assert set(urls(either)) == {"both", "python", "asyncio"}
    assert urls(both) == ["both"]
    assert either.total == 3


async def test_queries_are_stemmed_and_case_insensitive(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="The crawler is RUNNING the queues")
    await index_everything(database, stream)

    for query in ("run", "Runs", "queue", "crawlers QUEUE"):
        assert urls(await search(database, job_id, query)) == ["a"], query


async def test_phrases_and_exclusions_follow_websearch_syntax(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="phrase", body="consumer group recovery")
    await add_page(database, job_id, path="apart", body="group of consumer tools")
    await index_everything(database, stream)

    assert urls(await search(database, job_id, '"consumer group"')) == ["phrase"]
    assert urls(await search(database, job_id, "consumer -recovery")) == ["apart"]


async def test_equal_scores_are_ordered_by_url_then_page_id(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    for path in ("c", "a", "b"):
        await add_page(database, job_id, path=path, body="identical words here")
    await index_everything(database, stream)

    first = await search(database, job_id, "identical")
    again = await search(database, job_id, "identical")

    assert urls(first) == ["a", "b", "c"]
    assert len({hit.rank for hit in first.hits}) == 1
    assert [hit.page_id for hit in first.hits] == [hit.page_id for hit in again.hits]


# --- Paging --------------------------------------------------------------------------------------


async def test_pages_of_results_are_stable_and_complete(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    for index in range(7):
        await add_page(database, job_id, path=f"p{index}", body="paging " * (index + 1))
    await index_everything(database, stream)

    pages = [
        await search(database, job_id, "paging", limit=3, offset=offset) for offset in (0, 3, 6)
    ]
    everything = await search(database, job_id, "paging", limit=50)

    assert [len(page.hits) for page in pages] == [3, 3, 1]
    assert {page.total for page in pages} == {7}
    assert [hit.page_id for page in pages for hit in page.hits] == [
        hit.page_id for hit in everything.hits
    ]
    beyond = await search(database, job_id, "paging", offset=MAX_OFFSET)
    assert (beyond.total, beyond.hits) == (7, [])


@pytest.mark.parametrize(
    ("query", "limit", "offset", "field", "code"),
    [
        ("", 10, 0, "q", "empty_query"),
        ("  \t ", 10, 0, "q", "empty_query"),
        ("x" * (MAX_QUERY_CHARS + 1), 10, 0, "q", "query_too_long"),
        ("ok", 0, 0, "limit", "invalid_limit"),
        ("ok", 51, 0, "limit", "invalid_limit"),
        ("ok", 10, -1, "offset", "invalid_offset"),
        ("ok", 10, MAX_OFFSET + 1, "offset", "invalid_offset"),
    ],
)
async def test_invalid_queries_and_paging_are_refused_before_any_sql(
    database: Database, query: str, limit: int, offset: int, field: str, code: str
) -> None:
    with pytest.raises(InvalidInputError) as refused:
        await search(database, uuid.uuid4(), query, limit=limit, offset=offset)

    [violation] = refused.value.violations
    assert (violation.loc, violation.type) == (("query", field), code)


# --- Queries without searchable terms, special characters, no matches ---------------------------


@pytest.mark.parametrize("query", ["the", "the and of", "-python", "-a -b", "!!!", "&|"])
async def test_a_query_without_a_term_to_look_up_searches_nothing(
    database: Database, stream: IndexStream, query: str
) -> None:
    """Stop words only, or only excluded words: such a query would match every page."""
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="the python and the rest")
    await index_everything(database, stream)

    results = await search(database, job_id, query)

    assert (results.searchable, results.total, results.hits) == (False, 0, [])


@pytest.mark.parametrize(
    "query",
    [
        "c++ & (foo | !bar)",
        "'; DROP TABLE pages; --",
        '"unterminated phrase',
        "<-> :* \\ % _",
        "\U0001f600 emoji",
        "中文",
    ],
)
async def test_special_characters_never_break_a_search(
    database: Database, stream: IndexStream, query: str
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="foo bar table pages")
    await index_everything(database, stream)

    results = await search(database, job_id, query)

    assert results.total == len(results.hits)
    async with database.session() as session:  # nothing was harmed
        assert await session.scalar(text("SELECT count(*) FROM pages")) == 1


async def test_no_match_is_an_empty_result(database: Database, stream: IndexStream) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="redis")
    await index_everything(database, stream)

    results = await search(database, job_id, "zookeeper")

    assert (results.searchable, results.total, results.hits) == (True, 0, [])


async def test_an_unknown_job_is_not_found(database: Database) -> None:
    with pytest.raises(NotFoundError):
        await search(database, uuid.uuid4(), "redis")


# --- Visibility, isolation, freshness ------------------------------------------------------------


async def test_only_documents_whose_index_reflects_the_current_content_are_returned(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="indexed", body="visible")
    stale = await add_page(database, job_id, path="stale", body="visible")
    await index_everything(database, stream)
    await add_page(database, job_id, path="unindexed", body="visible")  # never indexed
    failed = await add_page(database, job_id, path="failed", body="visible")
    await execute(
        database,
        "UPDATE search_documents SET status = 'failed', last_error = 'x' WHERE page_id = :id",
        id=failed,
    )
    await change_content(database, stale, body="visible but replaced")  # indexed v1, page v2

    results = await search(database, job_id, "visible")

    assert urls(results) == ["indexed"]
    assert results.indexing.pending == 2  # "stale" and "unindexed"
    assert results.indexing.indexed == 1
    assert results.indexing.failed == 1
    await index_everything(database, stream)
    assert urls(await search(database, job_id, "visible")) == ["indexed", "stale", "unindexed"]
    assert urls(await search(database, job_id, "replaced")) == ["stale"]


async def test_a_reindexed_document_stays_searchable_until_it_is_indexed_again(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="durable")
    await index_everything(database, stream)
    await finish_job(database, job_id)

    async with database.session() as session:
        assert await request_reindex(session, job_id) == 1

    assert urls(await search(database, job_id, "durable")) == ["a"]


async def test_a_search_never_returns_another_jobs_pages(
    database: Database, stream: IndexStream
) -> None:
    first, _ = await new_job(database)
    second, _ = await new_job(database, host="other.test")
    mine = await add_page(database, first, path="a", body="shared words")
    await add_page(database, second, path="a", body="shared words", host="other.test")
    await index_everything(database, stream)

    results = await search(database, first, "shared")

    assert [hit.page_id for hit in results.hits] == [mine]
    assert results.total == 1


async def test_hits_carry_title_url_snippet_and_freshness(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    body = "Intro text. " * 40 + "The scheduler dispatches crawl tasks to the stream. " + "x " * 50
    page_id = await add_page(database, job_id, path="doc", title="Scheduler", body=body)
    await index_everything(database, stream)

    [hit] = (await search(database, job_id, "dispatch")).hits

    assert (hit.page_id, hit.url, hit.title) == (page_id, "https://example.test/doc", "Scheduler")
    assert hit.content_version == 1
    assert hit.indexed_at >= hit.fetched_at
    snippet = hit.snippet
    assert "dispatches" in snippet.text
    assert [snippet.text[start:end] for start, end in snippet.highlights] == ["dispatches"]
    assert "<" not in snippet.text  # plain text, never HTML


async def test_marker_characters_in_page_text_cannot_fake_highlights(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="\x02fake\x03 marker and real target")
    await index_everything(database, stream)

    [hit] = (await search(database, job_id, "target")).hits

    assert "\x02" not in hit.snippet.text
    assert "\x03" not in hit.snippet.text
    assert [hit.snippet.text[start:end] for start, end in hit.snippet.highlights] == ["target"]


async def test_a_page_without_body_gets_its_snippet_from_the_description(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", title="T", description="Consumer groups explained")
    await index_everything(database, stream)

    [hit] = (await search(database, job_id, "consumer")).hits

    assert hit.snippet.text == "Consumer groups explained"


async def test_a_search_that_runs_too_long_is_stopped(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The statement timeout maps to SearchTimeoutError (the query is replaced by a slow one)."""
    job_id, _ = await new_job(database)
    monkeypatch.setattr(search_module, "_QUERY_TERMS", text("SELECT pg_sleep(1) IS NULL"))

    with pytest.raises(SearchTimeoutError):
        async with database.session() as session:
            await search_job(session, job_id, "anything", timeout_seconds=0.05)


async def test_a_search_the_server_does_not_stop_is_stopped_by_the_service(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a server-side limit (the statement is replaced), the service's own deadline stops
    the search; before the review, the client-side timeout surfaced as a generic error."""
    job_id, _ = await new_job(database)
    monkeypatch.setattr(search_module, "_STATEMENT_TIMEOUT", text("SELECT CAST(:timeout AS text)"))
    monkeypatch.setattr(search_module, "_QUERY_TERMS", text("SELECT pg_sleep(2) IS NULL"))

    began = time.monotonic()
    with pytest.raises(SearchTimeoutError):
        async with database.session() as session:
            await search_job(session, job_id, "anything", timeout_seconds=0.3)

    assert time.monotonic() - began < 1.5
    async with database.session() as session:  # the connection is usable again
        assert await session.scalar(text("SELECT 1")) == 1


async def test_results_and_progress_come_from_one_snapshot(
    database: Database, stream: IndexStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another transaction indexes a matching page between the progress count and the search:
    neither part of the result sees it (REPEATABLE READ). At READ COMMITTED the search would
    return a page the reported progress does not count."""
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="snapshot")
    await index_everything(database, stream)
    late = await add_page(database, job_id, path="b", body="snapshot")
    original = read_job_indexing  # the function the search service calls

    async def read_then_index_another(session: Any, job: uuid.UUID) -> IndexingProgress | None:
        progress = await original(session, job)
        await execute(
            database,
            "UPDATE search_documents AS d SET status = 'indexed', indexed_content_version = 1,"
            " indexed_at = clock_timestamp(),"
            " search_vector = to_tsvector('english', p.extracted_text)"
            " FROM pages AS p WHERE p.id = d.page_id AND d.page_id = :id",
            id=late,
        )
        return progress

    monkeypatch.setattr(search_module, "read_job_indexing", read_then_index_another)
    results = await search(database, job_id, "snapshot")
    monkeypatch.undo()

    assert (urls(results), results.total) == (["a"], 1)
    assert (results.indexing.indexed, results.indexing.pending) == (1, 1)
    assert urls(await search(database, job_id, "snapshot")) == ["a", "b"]  # committed meanwhile
