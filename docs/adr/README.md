# Architecture Decision Records

Each record briefly covers one significant decision: its context, the decision, the consequences, and
the alternatives that were considered.

Conventions:

- One decision per file, numbered in sequence: `NNNN-short-title.md`.
- Accepted ADRs are never rewritten. A later ADR supersedes an earlier one, and the earlier one's
  status changes to "Superseded by ADR-NNNN".
- Status values: Proposed, Accepted, Superseded, Deprecated.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-postgresql-source-of-truth.md) | PostgreSQL is the source of truth; Redis Streams only delivers work | Accepted |
| [0002](0002-transactional-outbox.md) | Transactional outbox for publishing to Redis | Accepted |
| [0003](0003-at-least-once-leases-fencing.md) | At-least-once processing with leases, fencing tokens, and idempotent effects | Accepted |
| [0004](0004-level-synchronous-bfs.md) | Level-synchronous BFS with scheduler-side admission | Accepted |
| [0005](0005-postgresql-full-text-search.md) | PostgreSQL full-text search instead of a dedicated search engine | Accepted |
| [0006](0006-target-allowlist-ssrf-defenses.md) | Target allowlist and layered SSRF defenses | Accepted |
| [0007](0007-modular-monolith-process-roles.md) | Modular monolith with separate process roles | Accepted |

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
