# ADR-0011: Fencing tokens are issued only by claims

- Status: Accepted
- Date: 2026-11-06
- Extends: [ADR-0010](0010-database-enforced-fencing.md)

## Context

ADR-0010 made the database reject stale results through CHECK constraints on `result_epoch`,
plus a trigger that forbids any decrease of `lease_epoch`. The Phase 2C review found two kinds
of buggy statement that the database still accepted. Both were demonstrated on a disposable
database migrated to `0001`.

1. **A lease granted without a new token.** A claim or takeover that forgets
   `lease_epoch = lease_epoch + 1` leaves the previous holder's token valid. In the
   demonstration, worker-a claimed a URL (epoch 1) and stalled. Worker-b then took the expired
   lease with such a buggy statement, and the epoch stayed at 1. Worker-a's *correctly fenced*
   commit (`WHERE state = 'in_progress' AND lease_epoch = 1`) succeeded. Worker-b, the real
   lease holder, was fenced out.
2. **A token issued outside a claim.** A stale worker recorded its outcome with
   `lease_epoch = lease_epoch + 1, result_epoch = lease_epoch + 1`. Every CHECK constraint
   holds, so the stale result replaced the current lease.

CHECK constraints see only the new version of a row. They cannot express rules about
transitions, which compare old and new values.

## Decision

Migration `0002` adds the trigger `crawl_urls_lease_epoch_per_claim`, an `AFTER UPDATE` row
trigger on `crawl_urls`. It enforces two rules:

- **`lease_epoch` changes only in a claim.** The row moves from `queued` or `in_progress` to
  `in_progress`, and the token advances by exactly one.
- **Every lease grant needs that new token.** A grant is entering `in_progress`, or handing an
  `in_progress` row to a different `lease_owner`.

As an AFTER trigger it runs after the CHECK constraints, so a statement that also violates a
CHECK still reports that constraint. The rules of ADR-0010 are unchanged, and the `0001`
trigger that rejects decreases stays in place.

## Consequences

- **Tokens identify grants.** `lease_epoch` now counts the lease grants of a URL. Every grant
  issues a token that no earlier holder has, and nothing else changes it. Regression tests show
  that each rejected statement above was accepted under `0001`. Other tests show that the
  legitimate transitions still work: claims, takeovers, results, retry releases, and
  cancellation.
- **Renewals remain possible.** The same owner may extend `lease_expires_at` without a new
  token. ADR-0003 rejects heartbeats for now. If they are ever added, a renewal must be fenced
  with `WHERE lease_epoch = :token`.
- **Residual limits.** No constraint can catch the following; the worker's canonical statements
  and their tests must:
  - **A copied column instead of the token.** A statement that writes `result_epoch = lease_epoch`
    instead of its own token satisfies every rule. The database cannot know a worker's token
    unless the worker sends it.
  - **A same-owner re-grant.** A grant to the same `lease_owner` without a new token looks like
    a renewal. Consumer names must therefore be unique per process start.
  - **An unchecked fenced update.** A worker that inserts its page and then ignores a fenced
    `UPDATE` that matched no row would commit the page without an outcome. The result
    transaction must lock and verify the row first, and roll back on a mismatch (architecture
    section 9). A database guard, for example pages that carry their result epoch, can be added
    together with the worker (Phase 5) if wanted.
  - **Lease expiry.** The trigger does not check expiry. A time comparison in a trigger would
    race with the claim's own reading of the clock, so the claim's `WHERE` clause enforces it.
  - **Superuser connections.** In the development container the application connects as the
    database owner, which is a superuser and could disable triggers. A production deployment
    should connect as a role that is not a superuser.
- **Cost.** The trigger's `WHEN` clause filters out updates that cannot grant a lease or change
  the token. Results, releases and publication updates therefore queue no trigger event at all.

## Alternatives considered

- **Claim and commit as PL/pgSQL functions:** stronger guarantees, but application logic moves
  into stored procedures (also rejected in ADR-0010).
- **A session variable carrying the worker's token, checked by a trigger:** it closes the
  copied-column hole only if every statement sets the variable, and it adds hidden coupling.
- **Replacing the `0001` trigger:** a larger change to an applied migration's objects. The
  additive trigger leaves `0001` untouched.
