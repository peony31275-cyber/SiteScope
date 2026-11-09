# ADR-0025: Container images and a single-entry Compose deployment

- Status: Accepted
- Date: 2026-11-08
- Extends: [ADR-0007](0007-modular-monolith-process-roles.md) (process roles) and
  [ADR-0021](0021-operational-metrics-and-process-heartbeats.md) (heartbeats)

## Context

Through Phase 10, SiteScope ran from a development checkout. PostgreSQL and Redis ran in
containers, and every Python role ran from `backend\.venv` on Windows. Phase 11 has to deliver:

- a reproducible deployment of the whole stack, with images for every role;
- a single public entry, with PostgreSQL and Redis private;
- a controlled migration step;
- health checks and graceful shutdown;
- no secret in an image or in the repository.

The development database volumes must never be touched by it.

## Decision

1. **One backend image for every Python role.** The API, the scheduler, crawler workers, the
   index dispatcher, indexers, the migration step and the operator commands each get their
   command from `deploy/compose.yaml`. `backend/Dockerfile` is multi-stage:
   - The build stage installs the Linux lock `requirements/runtime.txt` into `/opt/venv`, with
     `--no-deps` and then `pip check`. `requirements/lock.py` regenerates the lock from
     `pyproject.toml`, constrained by the versions the tests ran with.
   - It byte-compiles the sources and removes pip.
   - The runtime image holds the virtual environment, the application, the migrations and
     nothing else. The build context is an allowlist (`.dockerignore`): no tests, no virtual
     environment, no `.env`.
   - It runs as uid 10001 with `SITESCOPE_ENVIRONMENT=production`.
2. **The frontend image is the public entry.** A Node stage runs `npm ci` from the lockfile and
   builds the dashboard. nginx serves it, unprivileged (uid 101, port 8080), and:
   - sends strict security headers (CSP `default-src 'self'`, no framing, `nosniff`);
   - forwards `/api/` to the API on the same origin, so there is no CORS;
   - never serves `/metrics`, `/docs` or `/openapi.json`;
   - applies the abuse limits of ADR-0024.
3. **The demo site is a container too.** nginx serves `demo-site/` (synthetic content written for
   SiteScope) as `http://demo-site:8080`. It is the only approved target by default: an exact
   internal origin, which production allows because it is a name, not an IP address.
4. **Two networks, one published port.**
   - `internal` (`internal: true`, so Docker gives it no gateway) holds every service but the
     proxy.
   - `edge` holds the proxy's published port: by default `127.0.0.1:8080`, on a server
     `0.0.0.0`.

   PostgreSQL, Redis, the API and the workers publish nothing. A crawler cannot reach the
   internet at all, so the network is defense in depth behind the fetcher's allowlist and SSRF
   rules (ADR-0006).
5. **Migrations run once, alone, as the owner, and the application is not the owner.**
   - A one-shot `migrate` service runs `alembic upgrade head` as the database owner. That is
     the superuser the PostgreSQL image creates, which backups and restores also use.
   - `alembic/env.py` takes an advisory lock (`MIGRATION_LOCK`) first in the migration
     transaction, so a second upgrade started concurrently waits, then finds nothing to do
     (tested with two processes).
   - Then the one-shot `db-roles` (`deploy/postgres/app-role.sql`, idempotent) gives the role
     `sitescope_app` its rights: rows of every table except `alembic_version`, no DDL, no
     superuser, its own password.
   - Every application service waits for both steps and connects as `sitescope_app`, as ADR-0011
     asked of production. A compromised process can no longer disable the fencing triggers
     (`session_replication_role`), change the schema, or run programs on the database server
     (`COPY ... PROGRAM`); each was refused in a test.
6. **Health is reported, not acted on.**
   - PostgreSQL: `pg_isready`. Redis: `PING`. The API: `GET /health`. The proxies: `/healthz`.
   - Each worker role: `python -m app.observability.alive <role>`, healthy while Redis holds a
     live heartbeat of that role from the container's own host name.
   - Compose uses health only to order startup (the proxy starts once the API is healthy).
     Heartbeats stay observability (ADR-0021): no claim, lease or dispatch reads them.
7. **Lifecycle.**
   - `init` (tini) for the Python roles, and `restart: unless-stopped`.
   - Stop grace periods longer than the roles' own: crawlers and indexers 45 s against
     `SITESCOPE_SHUTDOWN_GRACE_SECONDS` = 30.
   - Read-only root filesystems with a small `/tmp`, all capabilities dropped,
     `no-new-privileges`.
   - Memory limits, and rotated JSON-file logs.
   - Replicas of the API, crawlers and indexers set from `deploy/.env`.
8. **A separate Compose project.** The deployment is the project `sitescope-deploy` (validation
   runs use their own names), never the development project `sitescope`: its own containers,
   network and volumes.
9. **HTTPS is an overlay.** `deploy/compose.https.yaml` adds Caddy, which obtains a Let's Encrypt
   certificate and forwards to the proxy. In that variant nginx publishes nothing, and it
   trusts `X-Forwarded-For` from private addresses only (`deploy/nginx/real-ip.conf`).

## Consequences

- The same images run locally (WSL Docker) and on a VPS. The full stack starts with one command
  and was validated end to end (docs/deployment.md, "Validation").
- PostgreSQL and Redis are unreachable from outside. The API is reachable only through the proxy.
- One Compose host is a single point of failure. There is no orchestration across machines;
  losing Redis still loses no work (ADR-0001).
- Approving a public crawl target also needs egress for the crawlers. It is a deliberate,
  documented change (docs/deployment.md, "Crawling other sites"), never a default.
- The locks pin versions, not hashes, so one lock serves amd64 and arm64 images.
- Images are built where they run. No registry is involved, so publishing images is a later,
  separate decision.

## Alternatives considered

- **An image per role**: the same code built several times, with versions that could drift
  apart.
- **Kubernetes or Helm**: an orchestrator for one node and a portfolio demo.
- **Migrations at API startup**: replicas would race, and every start would depend on schema
  changes.
- **Publishing PostgreSQL or Redis for administration**: `docker compose exec` reaches them
  without any exposure.
- **Docker secrets files instead of environment variables**: the application reads its settings
  from the environment. On one host, anyone with Docker access can read either form.
- **The `nginx-unprivileged` image**: another base image to trust. The official image runs as
  uid 101 with the configuration here.
