# ADR-0009: Deduplicate URLs on the normalized URL itself

- Status: Accepted
- Date: 2026-11-06
- Amends: the deduplication key in [ADR-0004](0004-level-synchronous-bfs.md) and the Phase 0 data
  model

## Context

The Phase 0 design planned `UNIQUE (job_id, url_hash)`, where `url_hash` is the SHA-256 of the
normalized URL. The reason was size: PostgreSQL B-tree index entries are limited to roughly 2.7 kB,
and URLs can be long. The Phase 2B requirements specify `UNIQUE (job_id, normalized_url)`.

## Decision

- `crawl_urls` enforces `UNIQUE (job_id, normalized_url)` directly. Admission uses
  `INSERT ... ON CONFLICT (job_id, normalized_url) DO NOTHING`.
- A CHECK constraint enforces the normalization contract on every URL column:
  - `http` or `https`;
  - printable ASCII without spaces;
  - at most 2048 bytes.

  Index entries therefore always fit in a B-tree, which removes the original reason for a hash.
- URL columns use `COLLATE "C"`, i.e. byte order. This gives deterministic ordering for BFS
  admission, cheaper comparisons, and indexes that are immune to operating-system collation
  changes.
- `page_links.target_normalized_url` follows the same rules. A link's target status is found by
  joining `crawl_urls` on `(job_id, normalized_url)`.

## Consequences

- There is one source of truth for a URL, with no derived column that could disagree with it.
- The constraint is readable in error messages and query plans.
- The index is larger than one keyed on a 32-byte hash. That is acceptable at the planned scale of
  at most about 10^5 URLs.
- The Phase 2C URL normalizer must produce ASCII output (percent-encoding, IDNA hosts). The database
  rejects anything else.

## Alternatives considered

- **A SHA-256 hash column:** a smaller index, but a derived value the application must keep
  consistent.
- **An expression index on `md5(url)`:** still a derived key, and not collision-resistant.
