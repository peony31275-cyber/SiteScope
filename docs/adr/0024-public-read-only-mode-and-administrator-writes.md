# ADR-0024: A public deployment is read-only; writes need the administrator

- Status: Accepted
- Date: 2026-11-08
- Extends: [ADR-0006](0006-target-allowlist-ssrf-defenses.md) (where a crawl may go) and
  [ADR-0017](0017-retries-dead-letters-cancellation-idempotency.md) (job creation)

## Context

Until Phase 10, anyone who could reach the API could create and cancel crawl jobs. The crawl
scope (ADR-0006, ADR-0012) limits where a crawl may go, not how many crawls run. Each job costs
CPU, database rows, Redis traffic and requests to the target. On a public demo, any visitor
could have filled the queue, or cancelled the jobs the demo shows.

Phase 11 publishes a demo that anyone may browse. The demo needs:

- public reads: jobs, search, analysis and the link graph;
- writes reserved to its operator, enforced by the server;
- no user accounts, sessions, or secret held by the browser.

## Decision

1. **Write access is a setting with three modes** (`Settings.write_access`, derived and never
   configured directly):
   - `token` when `SITESCOPE_ADMIN_TOKEN` is set. A write must carry
     `Authorization: Bearer <token>`.
   - `disabled` in production without a token. Every write is refused.
   - `open` outside production without a token (development and tests, as before).

   In production a token must have at least 32 characters and must not be a placeholder.
2. **Only the two writes are checked**: `POST /api/v1/crawls` and
   `POST /api/v1/crawls/{id}/cancel`. A FastAPI dependency (`app/api/security.py`) runs the
   check before any service code:
   - `403 writes_disabled` in `disabled` mode;
   - `401 unauthorized` with `WWW-Authenticate: Bearer realm="sitescope"` for a missing or wrong
     token. The comparison runs in constant time (`hmac.compare_digest`).

   A refusal is logged (`api.write_refused`: reason, method, path, client), never the token.
   Everything else stays public and unchanged. The OpenAPI document declares the bearer scheme
   on these two operations only.
3. **The dashboard reads the mode, not the token.** `GET /api/v1/crawl-policy` reports
   `write_access`. Outside `open` mode the dashboard shows a read-only notice instead of the
   create form, and no cancel button. This is a convenience: the server enforces.
4. **The operator writes from inside the deployment.** `python -m app.scheduler.admin` (`create`,
   `cancel`, `seed-demo`) calls the services the API calls, so every rule of a crawl request
   still applies. It needs a shell with the deployment's configuration and database
   credentials, which is the operator's access already, so it takes no token. A read-only
   public demo is populated this way.
5. **At most `SITESCOPE_CRAWL_MAX_ACTIVE_JOBS` jobs are active** (queued, running or
   cancelling; default 50, 10 in `deploy/compose.yaml`). A new job beyond the cap is refused
   with `429 too_many_active_jobs` and `Retry-After: 60`.
   - A creation first takes a transaction-level advisory lock (`crawl_jobs.CREATION_LOCK`), and
     only then frees an expired key and inserts. A new job is counted with the active ones after
     its insert, and rolls back over the cap.
   - Concurrent creations serialize. A key that the scheduler's maintenance frees meanwhile
     cannot slip a job past the cap, and no creation holds a row while it waits for the lock.
     The review found both races in the first version, a check before the insert; each has a
     regression test.
   - A replay of an `Idempotency-Key` inserts nothing and is always answered.
   - The cap applies to the operator commands too.
6. **The public proxy limits each client address** (frontend/nginx/nginx.conf):
   - reads: 10 requests/s with bursts of 40;
   - writes: 10 a minute with bursts of 5, which slows down guessing a token;
   - 20 connections;
   - 16 KB request bodies;
   - short client timeouts.

   An excess request gets `429` with a problem document (`rate_limited`) and `Retry-After`.

## Consequences

- A public deployment without a token cannot be made to crawl anything, whatever a client sends.
  Even with a token, the active-jobs cap bounds the work queued at once.
- No user model, session, cookie or CSRF surface was added. The browser never holds a secret,
  in the bundle or at runtime.
- With a token, the dashboard is still read-only. The operator writes with an HTTP client and
  the token, or with the operator commands. This is deliberate (alternatives below).
- The token is one shared secret. To rotate it, change the environment and restart the API.
  Writes are not attributed to a person, only to a client address in the log.
- Proxy limits are per address. Clients behind one NAT share a budget. Behind a TLS proxy, the
  real address comes from that proxy (`deploy/nginx/real-ip.conf`, ADR-0025).

## Alternatives considered

- **User accounts** (sessions, OAuth): far larger than the need. They also bring password
  storage, CSRF and account management to a demo with one operator.
- **A token typed into the dashboard**, kept in browser storage: a secret in the frontend,
  exposed to any script injection. The bundle forbids raw HTML (ADR-0020), but the operator
  commands make this unnecessary anyway.
- **Hiding the form only**: not enforcement. Any HTTP client could still write.
- **An IP allowlist for writes at the proxy**: brittle (dynamic addresses, proxies). It also
  puts security policy in proxy configuration, where tests do not reach it.
- **Rate limits alone**: they slow down abuse but still let any visitor start crawls.
