# Phase 7: indexing throughput (1, 2 and 4 indexers) and search latency

Measured on 2026-11-08 (UTC) with `benchmarks/indexing.py`; method in
[`../README.md`](../README.md). These numbers describe this development machine and this
synthetic dataset only. They are not a capacity claim for any other setup, and they vary between
runs on this machine (see "Variation between runs").

## Environment

- AMD Ryzen 7 5800U laptop (8 cores, 16 threads), Windows 10 Home 10.0.19045; the WSL 2 VM sees
  16 CPUs and 7 GB of memory.
- Python 3.13.7 (`backend/.venv`). PostgreSQL 18.6 and Redis 8.10.3 in Docker inside WSL 2
  (Ubuntu 24.04), reached through WSL's localhost forwarding.
- Every process on the same machine: the index dispatcher, the indexers, the benchmark process,
  PostgreSQL and Redis.
- Dataset: one job of 2,000 pages, each with a 5-word title, a 15-word description, 20 words of
  headings and a 1,000-word body (pseudo-words from a 5,000-word vocabulary, Zipf-like frequency,
  seed 7). After indexing: `search_documents` 23 MB in total (with TOAST and indexes), its GIN
  index 6,344 kB.
- Indexers run 4 tasks each. The dispatcher publishes up to 1,000 tasks per pass every 0.1 s.

## Indexing

The final code, run 3. The clock starts at the commit that stores all 2,000 pages at once
(PostgreSQL's clock), so the per-page latency is mostly the time a page waited in that backlog.
Every run is listed; raw output: [`phase7-indexing.json`](phase7-indexing.json).

| Indexers (x4 tasks) | Runs: seconds to the last page | Runs: pages/s | Median pages/s | Runs: latency p50 / p95 (s) |
|---|---|---|---|---|
| 1 | 14.4, 15.7, 16.7 | 139.1, 127.3, 120.1 | 127.3 | 7.4 / 13.7, 8.1 / 15.0, 8.9 / 15.9 |
| 2 | 11.8, 11.6, 11.8 | 169.4, 172.7, 169.2 | 169.4 | 6.3 / 11.3, 6.1 / 11.0, 6.3 / 11.3 |
| 4 | 9.5, 9.4, 9.5 | 210.3, 213.3, 211.6 | 211.6 | 5.2 / 9.1, 5.0 / 9.0, 5.0 / 9.0 |

In every run each page was indexed on its first attempt (`attempt_count` 1), every message was
handled as `indexed`, the pages were spread evenly over the indexers (for example 495, 497, 503
and 505 with four), and every process stopped with exit code 0.

Throughput grows less than the number of indexers (1.33x with two, 1.66x with four, from the
medians). The benchmark does not show where the limit is; candidates are PostgreSQL itself, which
builds every vector (all processes share the machine's cores), and the two transactions plus one
Redis round trip each page costs. That would have to be measured (Phase 10) before any claim is
made.

### Variation between runs

The benchmark ran three times on 2026-11-08 (raw output of runs 1 and 2:
[`phase7-indexing-earlier-runs.txt`](phase7-indexing-earlier-runs.txt)):

| Run | Code | Median pages/s: 1, 2, 4 indexers |
|---|---|---|
| 1 (00:30 UTC) | before the independent review's fixes | 191.7, 221.4, 255.4 |
| 2 (01:36 UTC) | final, right after 25 minutes of full-load test runs | 130.7, 182.3, 225.2 |
| 3 (01:41 UTC) | final, the run reported above | 127.3, 169.4, 211.6 |

Of the review's changes, only one adds database work per page: the partial index
`ix_search_documents_open`. Interleaved runs with and without it (1 indexer, same dataset)
measured 139.8, 133.9 and 128.4 pages/s with it, and 136.0, 130.6 and 129.7 without it: no
difference. The other changes add no statement per page. The difference between run 1 and the
later runs is therefore most likely the machine's state. Within each sequence, throughput also
drifts down from run to run. The cause was not isolated. Treat these numbers as one laptop's
order of magnitude, not as a measure of the code's speed.

## Search

On the index of run 3 (2,000 pages), after `VACUUM (ANALYZE)`, each query ran 30 times through the
search service, one call at a time. A call is everything `search_job` does: a read-only
transaction, the job's indexing counts, the query check, and the search with snippets for 10
results.

| Query | Matches | ms min | ms p50 | ms p95 | ms max |
|---|---|---|---|---|---|
| a common term (in every page) | 2000 | 34.8 | 36.9 | 41.0 | 46.6 |
| a medium term | 1287 | 27.6 | 29.9 | 35.7 | 37.5 |
| a rare term | 45 | 17.5 | 18.8 | 19.6 | 20.3 |
| two terms | 1287 | 30.8 | 32.1 | 33.1 | 33.4 |
| an OR of two terms | 1301 | 29.1 | 30.0 | 30.9 | 31.4 |
| a phrase | 2 | 9.7 | 10.4 | 11.1 | 11.2 |
| no match | 0 | 6.5 | 7.2 | 7.8 | 7.9 |
| stop words only (not run) | 0 | 5.9 | 6.3 | 6.8 | 7.3 |

## Query plans

[`phase7-search-plans.txt`](phase7-search-plans.txt) holds `EXPLAIN (ANALYZE, BUFFERS)` of the
search statement, run 3:

- **Rare term** (45 matches): a bitmap scan of the GIN index `ix_search_documents_search_vector`,
  then the pages by primary key; 9.8 ms in all, most of it `ts_headline` for the 10 returned rows
  (about 0.9 ms each).
- **Common term** (all 2,000 pages): PostgreSQL chose a sequential scan of the documents, since
  every one matches, and ranked all 2,000 (about 19 ms of the 31 ms); `ts_headline` then ran for
  the 10 returned rows only.

In run 2, without the `VACUUM` step, the rare term was also searched with a sequential scan
(18 ms), most likely because right after bulk indexing the GIN index's pending list (its
fast-update entries) was not merged yet, which PostgreSQL's cost estimate counts against an index
scan. Autovacuum merges it in normal operation.

The cost of a search grows with its number of matches, because every match is ranked before a page
of results is cut (architecture, section 13).
