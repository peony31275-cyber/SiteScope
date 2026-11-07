# Architecture Decision Records

Each record briefly covers one significant decision: its context, the decision, the consequences, and
the alternatives that were considered.

Conventions:

- One decision per file, numbered in sequence: `NNNN-short-title.md`.
- Accepted ADRs are never rewritten. A later ADR supersedes an earlier one, and the earlier one's
  status changes to "Superseded by ADR-NNNN".
- A later ADR that changes only part of an earlier one is noted on the earlier one's status line
  ("superseded in part", "amended", "extended").
- Status values: Proposed, Accepted, Superseded, Deprecated.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-postgresql-source-of-truth.md) | PostgreSQL is the source of truth; Redis Streams only delivers work | Accepted; extended by 0016 |
| [0002](0002-transactional-outbox.md) | Transactional outbox for publishing to Redis | Accepted; index part superseded by 0008; extended by 0016 |
| [0003](0003-at-least-once-leases-fencing.md) | At-least-once processing with leases, fencing tokens, and idempotent effects | Accepted; extended by 0010, 0014 and 0016 |
| [0004](0004-level-synchronous-bfs.md) | Level-synchronous BFS with scheduler-side admission | Accepted; dedup key amended by 0009; extended by 0014 and 0016 |
| [0005](0005-postgresql-full-text-search.md) | PostgreSQL full-text search instead of a dedicated search engine | Accepted |
| [0006](0006-target-allowlist-ssrf-defenses.md) | Target allowlist and layered SSRF defenses | Accepted; extended by 0012 (allowlist), 0013 (fetcher) and 0015 (robots.txt, politeness) |
| [0007](0007-modular-monolith-process-roles.md) | Modular monolith with separate process roles | Accepted |
| [0008](0008-search-documents-as-index-outbox.md) | `search_documents` is both the index store and the indexing outbox | Accepted |
| [0009](0009-dedup-on-normalized-url.md) | Deduplicate URLs on the normalized URL itself | Accepted |
| [0010](0010-database-enforced-fencing.md) | Fencing is enforced by the database, not only by worker code | Accepted; extended by 0011 |
| [0011](0011-fencing-tokens-issued-only-by-claims.md) | Fencing tokens are issued only by claims | Accepted; extended by 0014 |
| [0012](0012-approved-crawl-targets.md) | Approved crawl targets are host patterns, plus allowed schemes and ports | Accepted |
| [0013](0013-safe-fetcher-pinned-connections.md) | The safe fetcher pins connections through httpcore2 and validates every hop | Accepted; extended by 0015 |
| [0014](0014-crawl-state-transactions-and-redirects.md) | Crawl state transactions, same-level redirects, and page directives | Accepted |
| [0015](0015-robots-txt-and-per-host-politeness.md) | robots.txt (RFC 9309) and per-host politeness through Redis | Accepted |
| [0016](0016-distributed-execution-redis-streams.md) | Distributed execution over Redis Streams: publication, acknowledgment, recovery | Accepted |

## Template

```markdown
# ADR-NNNN: Title

- Status: Proposed | Accepted | Superseded by ADR-NNNN
- Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```
