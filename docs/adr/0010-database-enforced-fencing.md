# ADR-0010: Fencing is enforced by the database, not only by worker code

- Status: Accepted. Extended by [ADR-0011](0011-fencing-tokens-issued-only-by-claims.md)
  (tokens are issued only by claims; it also lists what the database cannot detect).
- Date: 2026-11-06
- Extends: [ADR-0003](0003-at-least-once-leases-fencing.md)

## Context

In ADR-0003, workers fence their results with a conditional update:
`... WHERE state = 'in_progress' AND lease_epoch = :token`. If worker code ever omitted that
condition, a stale worker (one that paused past its lease while another worker took over) could
overwrite a newer result.

The owner's Phase 2B requirement is that the database must reject stale worker results at the
transaction level.

## Decision

`crawl_urls` records which lease produced each outcome, and the database enforces the rules:

- `result_epoch` is the lease epoch under which the current outcome was recorded.
  `CHECK (result_epoch IS NULL OR result_epoch = lease_epoch)` means an outcome can only be written
  under the current lease.
- `CHECK (state <> 'in_progress' OR result_epoch IS NULL)`: a claim must clear the previous outcome
  marker.
- Worker outcomes (`fetched`, `failed`, `skipped`) require `result_epoch IS NOT NULL`.
- A lease owner and lease expiry exist if and only if the state is `in_progress`.
- A trigger rejects any decrease of `lease_epoch`, so fencing tokens are monotonic.

A worker writes its page, links, search document and outcome (with `result_epoch = :token`) in one
transaction. If its token is stale, the CHECK fails and PostgreSQL rolls back the entire
transaction, including the page.

## Consequences

- **Defense in depth.** The conditional update stays the first line of defense, and a stale result
  still cannot persist even with buggy worker SQL. An integration test proves this: a worker that
  omits the fencing condition is rejected, and its page write is rolled back.
- **Stricter statements.** The claim and commit statements must clear and set `result_epoch`; the
  constraints ensure no statement can forget to.
- **Hidden logic.** The trigger is logic that lives in the database. It is documented in the
  migration header and covered by tests.
- **Late results.** A late result from the *current* lease epoch is still accepted, by design. If
  no other worker took over, the result is valid.

## Alternatives considered

- **Application-level conditional updates only (ADR-0003 as written):** one forgotten condition
  breaks fencing.
- **A composite foreign key `pages(crawl_url_id, lease_epoch) -> crawl_urls(id, lease_epoch)`:**
  covers only page rows, and needs an extra unique index.
- **Claim and commit as PL/pgSQL functions:** strong guarantees, but it moves application logic
  into stored procedures.
