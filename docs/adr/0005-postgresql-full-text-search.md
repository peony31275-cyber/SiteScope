# ADR-0005: PostgreSQL full-text search instead of a dedicated search engine

- Status: Accepted
- Date: 2026-11-06

## Context

SiteScope needs keyword search with relevance ranking. The corpus is small: crawled pages, up to about
10^5 per deployment. Search must stay consistent with crawl data and must be testable in CI.

## Decision

- **Index table.** A `search_documents` table with one row per page:
  - a weighted `tsvector` (title A, headings B, body C);
  - a GIN index on that vector.
- **Asynchronous indexing.** The indexer worker writes `search_documents` from the index stream
  ([ADR-0002](0002-transactional-outbox.md)).
- **Querying:**
  - `websearch_to_tsquery` parses the query;
  - `ts_rank_cd` ranks the results;
  - `ts_headline` builds snippets, with sentinel delimiters instead of HTML so highlighting stays safe.
- **Size limit.** Body text is truncated before indexing, because a `tsvector` is limited to 1 MB.

## Consequences

- **Fewer moving parts:**
  - no extra service to run;
  - search data is transactional and backed up with everything else;
  - search is easy to test.
- **Independent re-indexing.** Indexing is decoupled from crawling, so changing the index needs no
  re-crawl.
- **Basic ranking:**
  - no corpus statistics (no IDF or BM25);
  - no typo tolerance;
  - a single language configuration.
- **Latency is unproven.** Search latency at this scale must be measured in Phase 10, not assumed.

## Alternatives considered

- **Elasticsearch / OpenSearch:** BM25, analyzers, and scale-out, but heavy on memory and operations for
  this project.
- **Meilisearch / Typesense:** good relevance with little effort, but another stateful service that has
  to be kept consistent.
- **A generated `tsvector` column on `pages`:** the simplest option, but it puts GIN maintenance on the
  crawl write path and couples re-indexing to page rows.
