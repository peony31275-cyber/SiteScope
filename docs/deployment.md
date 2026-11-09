# Deployment

How to run SiteScope as a containerized stack on one host, and operate it as a public, read-only
demo. The design is in [ADR-0025](adr/0025-container-packaging-and-deployment-topology.md)
(images, topology) and [ADR-0024](adr/0024-public-read-only-mode-and-administrator-writes.md)
(write access, abuse limits). The threat model is in [security.md](security.md).

**Status:** the stack was built and validated end to end on the development machine (Docker
Engine 29.9.3 in WSL Ubuntu 24.04; section "Validation"). It has not been deployed to a public
host, and no hosting has been bought.

## Contents

1. [What runs](#what-runs)
2. [Choosing a host](#choosing-a-host)
3. [First start](#first-start)
4. [Configuration and secrets](#configuration-and-secrets)
5. [Operating the demo](#operating-the-demo)
6. [Upgrades, migrations and rollback](#upgrades-migrations-and-rollback)
7. [Backup and restore](#backup-and-restore)
8. [HTTPS and a domain](#https-and-a-domain)
9. [Logs, health and restarts](#logs-health-and-restarts)
10. [Crawling other sites](#crawling-other-sites)
11. [Server checklist](#server-checklist)
12. [Validation](#validation)

## What runs

`deploy/compose.yaml`, Compose project `sitescope-deploy` (never the development project
`sitescope`):

| Service | Image | Network | Published | Health check |
|---|---|---|---|---|
| `frontend` | `sitescope-frontend` (nginx 1.30.5, uid 101) | edge, internal | `DEPLOY_HTTP_BIND:DEPLOY_HTTP_PORT` (default `127.0.0.1:8080`) | `GET /healthz` |
| `api` (replicas: `DEPLOY_API_REPLICAS`) | `sitescope-backend` (Python 3.13.16, uid 10001) | internal | no | `GET /health` |
| `scheduler` | `sitescope-backend` | internal | no | own heartbeat |
| `crawler` (replicas: `DEPLOY_CRAWLER_REPLICAS`, default 2) | `sitescope-backend` | internal | no | own heartbeat |
| `index-dispatcher` | `sitescope-backend` | internal | no | own heartbeat |
| `indexer` (replicas: `DEPLOY_INDEXER_REPLICAS`, default 2) | `sitescope-backend` | internal | no | own heartbeat |
| `migrate` (one-shot, as the database owner) | `sitescope-backend` | internal | no | exits 0 |
| `db-roles` (one-shot) | `postgres:18.6-trixie` (psql) | internal | no | exits 0 |
| `postgres` | `postgres:18.6-trixie` | internal | no | `pg_isready` |
| `redis` | `redis:8.10.3-alpine3.23` (AOF, every second) | internal | no | `PING` |
| `demo-site` | `sitescope-demo-site` (nginx 1.30.5, uid 101) | internal | no | `GET /healthz` |

- The `internal` network is `internal: true`: Docker gives it no gateway, so nothing on it can
  reach the internet, and a crawler can fetch only the demo site.
- Only the proxy publishes a port. It serves the dashboard and forwards `/api/` to the API on the
  same origin. It never serves `/metrics`, `/docs` or `/openapi.json`.
- The application connects to PostgreSQL as `sitescope_app`. That role can read and write rows,
  nothing else: no DDL, no superuser, no access to the migration history
  (`deploy/postgres/app-role.sql`, applied by `db-roles` after every migration). Only `migrate`,
  backups and restores use the owner.
- Startup order: PostgreSQL healthy → `migrate` completes → `db-roles` completes → the
  application services → the API healthy → the proxy.

## Choosing a host

Prices as read on the providers' official pages on **2026-11-08**. Sources are listed below the
table, and nothing was bought or signed up for.

- **Q** marks a value quoted from a page.
- **A** marks our arithmetic or sizing assumption.

Hetzner prices are in EUR excluding VAT. DigitalOcean prices are in USD, and "taxes apply in
some countries". Render, Railway and Fly.io prices are in USD; their pages don't say how tax is
handled. Prices change: check them before deciding.

**Sizing (A).** The validated stack uses about 600 MiB of memory at rest after the demo crawl
(section "Validation"), and more while crawling and indexing. With the operating system, a
**4 GB** machine leaves room; 2 GB is tight.

| Option | Monthly cost | What it is | Main limits |
|---|---|---|---|
| **DigitalOcean Basic Droplet** (2 vCPU, 4 GiB, 80 GiB SSD, 4,000 GiB transfer) + Compose | **$28.80** (A): $24.00 (Q) + weekly backups at 20% (Q). Daily backups: 30% (Q). | A VPS: this repository's Compose file as is | IPv4 included (Q). You patch and monitor the OS. One host. |
| **Hetzner Cloud CPX22** (2 vCPU, 4 GB, 80 GB, 20 TB traffic) + Compose | **≈ €23.99** (A): €19.99 "incl. IPv4" (Q) + backups at 20% of the server price (Q) | A VPS, same as above | The cheaper CX23 (2 vCPU, 4 GB, €5.99) was "Currently not available" (Q). Prices for new orders rose on 15 Jun 2026 (Q). |
| **Railway Hobby** | **≈ $18–27** (A): $5 plan with $5 of usage included (Q). RAM $10/GB-month, vCPU $20/month, volumes $0.15/GB-month (Q), at an assumed 1.5–2 GB RAM | Managed containers, billed on use | Volumes at most 5 GB, one per service (Q). PostgreSQL 18 not confirmed in the docs. Backups on Hobby not confirmed. |
| **Fly.io** | **≈ $37** (A): 5 machines × 512 MB at $3.69, 4 × 256 MB at $2.19 (Q prices), self-run PostgreSQL and Redis with volumes | Managed VMs | No free tier (Q). Managed Postgres offers PostgreSQL 16 or 17 only (Q); SiteScope's schema needs 18 (`uuidv7()`, migration 0001). |
| **Render** (Hobby workspace) | **≈ $80.50** (A): 9 Starter instances at $7 (Q), Postgres Basic-256mb $6 + storage, Key Value 256 MB $10 (Q) | Fully managed | No free background workers (Q). Key Value is Valkey 8 (Q), which SiteScope has not been tested with. |
| **Oracle Cloud Always Free** (Ampere A1, Arm) | **$0** | A VM: "equivalent to 2 OCPUs and 12 GB" (Q) | Idle instances may be reclaimed (Q): an idle demo is at risk. Capacity errors happen (Q). Images must be Arm64. |

**Free tiers do not fit.** SiteScope needs several always-on background processes and a
persistent PostgreSQL. Render's free web services spin down after 15 minutes, its free
PostgreSQL expires after 30 days, and its free Key Value has no persistence (Q). Railway's free
plan allows 3 services of 0.5 GB with $1 of credit (Q).

**Recommendation: one 4 GB VPS running this Compose stack.**

- It is the cheapest always-on option: about $29 a month at DigitalOcean with weekly backups,
  or about €24 at Hetzner.
- It runs exactly what the tests ran: PostgreSQL 18.6 and Redis 8.10.3 with AOF.
- The nine long-running processes cost nothing extra.
- It is private by construction, with one public entry.

The costs of this choice: you patch the OS, renew nothing by hand (Caddy renews certificates),
watch disk and memory, and keep off-host backups. One host is a single point of failure;
losing Redis loses no work (ADR-0001), losing the disk loses whatever the last backup lacks.

The managed runner-up is **Railway** (about $18–27 a month on measured use). Choose it only after
confirming that its PostgreSQL template runs version 18 and that Hobby includes backups.

<details>
<summary>Sources (read on 2026-11-08)</summary>

- Hetzner: <https://www.hetzner.com/cloud/>, <https://www.hetzner.com/cloud/cost-optimized>,
  <https://www.hetzner.com/cloud/regular-performance>,
  <https://docs.hetzner.com/cloud/servers/primary-ips/overview/>,
  <https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/>,
  <https://www.hetzner.com/pressroom/standardization-and-price-adjustment-of-our-server-products/>
- DigitalOcean: <https://www.digitalocean.com/pricing/droplets>,
  <https://docs.digitalocean.com/products/backups/details/pricing/>
- Oracle: <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Render: <https://render.com/pricing>, <https://render.com/docs/free>,
  <https://render.com/docs/background-workers>, <https://render.com/docs/private-network>,
  <https://render.com/docs/key-value>, <https://render.com/docs/postgresql-creating-connecting>,
  <https://render.com/docs/postgresql-backups>
- Railway: <https://railway.com/pricing>, <https://docs.railway.com/reference/pricing/plans>,
  <https://docs.railway.com/reference/pricing/free-trial>,
  <https://docs.railway.com/reference/volumes>, <https://docs.railway.com/reference/backups>,
  <https://docs.railway.com/reference/private-networking>,
  <https://docs.railway.com/databases/postgresql>
- Fly.io: <https://docs.fly.io/about/pricing>, <https://docs.fly.io/mpg/>,
  <https://docs.fly.io/networking/private-networking/>,
  <https://docs.fly.io/launch/autostop-autostart/>

</details>

## First start

On the server, with Docker Engine and the Compose plugin (2.24 or newer) installed, from a
checkout of this repository:

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Set DEPLOY_POSTGRES_OWNER_PASSWORD, SITESCOPE_POSTGRES_PASSWORD and SITESCOPE_REDIS_PASSWORD,
# each to its own value from:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose -f deploy/compose.yaml up -d --build --wait
docker compose -f deploy/compose.yaml exec api \
    python -m app.scheduler.admin seed-demo --base-url http://demo-site:8080 --wait 600
python3 deploy/smoke.py http://127.0.0.1:8080 --read-only --demo
```

- `up --build --wait` builds the three images and starts everything in order. It returns once
  every service is healthy and `migrate` has exited successfully.
- `seed-demo` creates the demo jobs and waits until they are crawled and indexed (section
  "Operating the demo").
- `smoke.py` checks the public entry: headers, the API, refused writes, hidden internal
  endpoints, and the demo data. It uses the standard library only and changes nothing.

The dashboard is then on `http://127.0.0.1:8080`. To publish it, set `DEPLOY_HTTP_BIND=0.0.0.0`
(plain HTTP), or better, add HTTPS (section "HTTPS and a domain").

## Configuration and secrets

Everything is read from `deploy/.env` (template: `deploy/.env.example`). Compose reads it
because it sits next to the Compose file, and puts the values into the containers'
environments.

- The admin token goes to the API alone.
- The application role's password goes to the backend roles and `db-roles`.
- The owner's password goes to PostgreSQL, `migrate` and `db-roles`.
- Every backend role gets Redis' password: the application refuses to start in production
  without it.

- **Secrets:**
  - required: `DEPLOY_POSTGRES_OWNER_PASSWORD` (the database owner: migrations and backups),
    `SITESCOPE_POSTGRES_PASSWORD` (the application role `sitescope_app`) and
    `SITESCOPE_REDIS_PASSWORD`;
  - optional: `SITESCOPE_ADMIN_TOKEN`.
  - Generate each one; never reuse the development values. In production the application
    refuses placeholder values (the `change-me` prefix), and admin tokens shorter than 32
    characters.
  - `deploy/.env` is git-ignored. Keep it `chmod 600` and in a password manager, never in a
    repository, an image or a chat. Anyone in the `docker` group can read the containers'
    environment: that group is root-equivalent.
- **Write access** (ADR-0024): leave `SITESCOPE_ADMIN_TOKEN` empty for a read-only public demo.
  Set it to accept writes with `Authorization: Bearer <token>`. Rotate it by changing the value
  and recreating the API (`docker compose -f deploy/compose.yaml up -d api`).
- **Crawl policy:** by default `demo-site` on port 8080 only, at most 200 pages and depth 5 per
  job, and at most 10 active jobs. The application validates every value at startup; production
  refuses `localhost` and IP addresses.
- **Scale:** `DEPLOY_API_REPLICAS`, `DEPLOY_CRAWLER_REPLICAS`, `DEPLOY_INDEXER_REPLICAS`.
  Each Python process holds up to 10 database connections (pool 5, overflow 5). PostgreSQL's
  default of 100 allows about 9 such processes, and the default scale runs 7. Raise
  `max_connections` before running more.
- **Images:** `DEPLOY_BACKEND_IMAGE`, `DEPLOY_FRONTEND_IMAGE`, `DEPLOY_DEMO_SITE_IMAGE`. They
  default to local builds (`:local`). Tag a release (`:<git-sha>`) to be able to roll back.

## Operating the demo

The operator commands run inside the stack and need no token (ADR-0024). They apply every rule
of a crawl request: approved targets, limits, the active-jobs cap.

```sh
DC="docker compose -f deploy/compose.yaml"
$DC exec api python -m app.scheduler.admin seed-demo --base-url http://demo-site:8080 --wait 600
$DC exec api python -m app.scheduler.admin create --seed http://demo-site:8080/docs/ --max-depth 2 --max-pages 50
$DC exec api python -m app.scheduler.admin cancel <job-id>
$DC exec api python -m app.scheduler.status              # crawl and index state, live processes
$DC exec api python -m app.scheduler.search <job-id> "polite crawler"
```

`seed-demo` creates four jobs, the full crawl last so that it is the newest:

| Job | Seed | Depth | Page limit | Shows |
|---|---|---|---|---|
| `docs` | `/docs/` | 2 | 50 | one section, from its index page |
| `blog` | `/blog/` | 1 | 20 | depth 1: the seed and the pages it links to |
| `budget` | `/` | 3 | 6 | a page budget smaller than the site: exactly 6 URLs admitted |
| `site` | `/` | 3 | 100 | the whole site: cycles, robots.txt rules, a noindex page, a nofollow page, a broken link |

- **The content is synthetic.** The demo site (`demo-site/`) is a small website written for
  SiteScope; its pages say so. Nothing in the demo comes from a real website.
- **Re-running creates nothing.** An equal job (same seed, depth and limit) that is queued,
  running or completed stands for its demo; a cancelled or failed one is replaced.
- **Metrics stay internal** (`GET /metrics` is not proxied). Read them from inside:
  `$DC exec api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"`.

## Upgrades, migrations and rollback

**Upgrade:**

```sh
$DC exec -T postgres pg_dump -U sitescope -d sitescope -Fc > backups/before-upgrade.dump
git pull            # or check out a release tag
$DC build
$DC up -d --wait
```

`up` recreates the services whose image changed. `migrate` runs `alembic upgrade head` first,
alone, and the application services wait for it to succeed. A second, concurrent upgrade waits
on the migration lock, then finds nothing to do (`alembic/env.py`). A failed migration rolls back
completely (transactional DDL): fix the cause and run `up` again.

**Rollback:**

- If the release added no migration, check out the previous release, `build` and `up -d`.
  Pinned image tags (`DEPLOY_*_IMAGE=...:<git-sha>`) make this quicker.
- If it did add one, the old code may not run against the new schema. Restore the backup taken
  before the upgrade (next section) and start the previous release. Every migration has a
  downgrade, but a downgrade can drop columns and their data: use it only on purpose, after a
  backup.

## Backup and restore

PostgreSQL holds all the state (ADR-0001). Redis needs no backup: it holds deliverable messages,
rate limits, caches and heartbeats, and the reconciler republishes from PostgreSQL whatever it
lost.

**Backup** (custom format, compressed):

```sh
mkdir -p backups
$DC exec -T postgres pg_dump -U sitescope -d sitescope -Fc > backups/sitescope-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Copy the dump off the host (object storage, another machine). Provider snapshots (DigitalOcean
and Hetzner backups) are a second layer, not a replacement: a snapshot of a running database
volume is only crash-consistent.

**Check a backup without touching the live database.** Restore it into a scratch database, count
the jobs, then drop the scratch database:

```sh
$DC exec -T postgres createdb -U sitescope sitescope_restore_check
$DC exec -T postgres pg_restore -U sitescope -d sitescope_restore_check --no-owner < backups/<file>.dump
$DC exec -T postgres psql -U sitescope -d sitescope_restore_check -c "SELECT status, count(*) FROM crawl_jobs GROUP BY 1"
$DC exec -T postgres dropdb -U sitescope sitescope_restore_check
```

**Restore** (replaces the live data; stop the writers first):

```sh
$DC stop api scheduler crawler index-dispatcher indexer
$DC exec -T postgres pg_restore -U sitescope -d sitescope --clean --if-exists --no-owner < backups/<file>.dump
$DC up -d --wait
```

## HTTPS and a domain

1. Point an A record for your domain at the server. Add an AAAA record only if IPv6 is
   enabled in Docker and on the `edge` network. Otherwise IPv6 visitors reach Caddy through
   Docker's userland proxy, all from one address, and share one rate-limit budget.
2. Open ports 80 and 443 in the firewall. Port 80 serves the ACME challenge and redirects to
   HTTPS.
3. Set `DEPLOY_DOMAIN` and `DEPLOY_ACME_EMAIL` in `deploy/.env`.
4. Start with the HTTPS overlay:

   ```sh
   docker compose -f deploy/compose.yaml -f deploy/compose.https.yaml up -d --build --wait
   ```

Caddy (`deploy/Caddyfile`) obtains and renews a Let's Encrypt certificate and adds HSTS. It
keeps the certificate and the ACME account in the `caddy-data` volume, which you should keep:
Let's Encrypt rate-limits new certificates. In this variant nginx publishes no port, and it
takes the client address from Caddy's `X-Forwarded-For` (`deploy/nginx/real-ip.conf`), so the
rate limits apply per client (per IPv4 client: see step 1). Caddy publishes UDP 443 too, for
HTTP/3.

### Behind another proxy

The same applies to any other proxy in front of nginx on the same host (a host nginx, a
tunnel). Without `real-ip.conf`, nginx sees every request coming from that proxy, through
Docker's gateway address: one rate-limit budget for every visitor, and one address in the logs.
Mount the file in a Compose override of your own, as `compose.https.yaml` does:

```yaml
services:
  frontend:
    volumes:
      - ./nginx/real-ip.conf:/etc/nginx/conf.d/real-ip.conf:ro
```

Then make the proxy set `X-Forwarded-For`. nginx trusts that header from private addresses
only, so expose the published port to that proxy alone (`DEPLOY_HTTP_BIND=127.0.0.1`).

## Logs, health and restarts

- **Logs:** `$DC logs -f api` (or any service). Every service logs to stdout, the Python roles
  as JSON lines (request IDs, job and worker IDs). Docker keeps three files of 10 MB per
  container.
- **Proxy logs.**
  - The access log records the request path without its query string, so search terms stay
    out of it. Rate-limit rejections appear there as 429s, and are not written to the error
    log.
  - nginx's error log can still contain a request line, query string included, when the API
    cannot be reached (an upstream error).
- **Health:** `$DC ps` shows each container's health.
  - A worker is healthy while its own heartbeat is live in Redis
    (`python -m app.observability.alive <role>`). An unhealthy worker is frozen, has lost
    Redis, or has stopped.
  - Docker reports health only. It restarts a container when its process exits, not when it
    turns unhealthy.
- **Restarts:** `restart: unless-stopped`. A crashed process restarts. Its leases expire, and
  another worker takes its URLs over (fencing, ADR-0010).
- **Stopping:** `$DC stop` sends SIGTERM. Workers stop reading and finish their tasks for up to
  30 s; Docker waits 45 s before SIGKILL.

## Crawling other sites

The default deployment can crawl only the bundled demo site, and the network enforces it: the
crawlers have no route to the internet. Crawling a real site takes three deliberate changes:

1. Approve it: `SITESCOPE_CRAWL_ALLOWED_HOSTS` (and schemes and ports).
2. Give the crawlers egress, in a Compose override that you review:

   ```yaml
   services:
     crawler:
       networks: [internal, egress]
   networks:
     egress: {}
   ```

3. Make sure you are allowed to. SiteScope obeys robots.txt and paces requests per host, but
   permission is yours to have.

The fetcher's defenses still apply with egress (ADR-0006, ADR-0013): addresses checked after DNS
resolution, connections pinned to them, private and loopback addresses refused unless they
are an exact internal origin.

## Server checklist

- A firewall that admits SSH, 80 and 443 only. Docker publishes nothing else here, and
  PostgreSQL and Redis are not on a published network at all.
- SSH keys only, automatic security updates, and time synchronization. Rate limits use Redis'
  clock and leases PostgreSQL's, but logs need the right time.
- `deploy/.env` at `chmod 600`, owned by the deploying user.
- A regular `pg_dump` copied off the host, and a restore check (section "Backup and restore").
- Disk monitoring: PostgreSQL's volume, Redis' AOF, and Docker's logs and images
  (`docker system df`).

## Validation

Performed on the development machine on 2026-11-08: Windows 10, Docker Engine 29.9.3 and Compose
v5.7.0 in WSL Ubuntu 24.04. Two runs:

1. **Validation** (project `sitescope-validate`, port 18080), on the working tree.
2. **Walkthrough** (project `sitescope-walkthrough`, port 8080). It used a fresh copy of exactly
   the 506 files to be committed, in WSL's own filesystem, and followed "First start" with three
   generated passwords. It ran after the application database role was added; the validation
   run predates it.

Both used throwaway secrets kept outside the repository. Afterwards each project's containers,
networks and two volumes were removed. The development project and its volumes were not
touched. Times depend on the machine.

| Check | Result |
|---|---|
| `compose config` (base, and with the HTTPS overlay) | valid. With the overlay only Caddy publishes ports (80, 443), and nginx mounts `real-ip.conf` |
| Image builds (base images present) | 38.6 s. Backend image 312 MB (virtual environment 99 MB), frontend 94.1 MB, demo site 92.9 MB |
| `up -d --wait` | validation: all 11 long-running containers healthy in 25.6 s (images built). Walkthrough, `up -d --build --wait` from the fresh copy: 66.1 s, with `migrate` and `db-roles` exited 0. `migrate` applied 0001–0005 |
| Published ports | only the frontend, on `127.0.0.1:18080` |
| Egress from a crawler | `1.1.1.1:443` and `8.8.8.8:53`: `Network is unreachable`. `example.com` does not resolve; `demo-site` does |
| `seed-demo --wait 600` | 31.8 s, then 32.1 s in the walkthrough. Four jobs completed and fully indexed. The full crawl: 14 URLs admitted, 12 pages, 11 indexed (the integration test's numbers) |
| `deploy/smoke.py --read-only --demo` (walkthrough) | 17 of 17 checks passed |
| Database roles (walkthrough) | The application connects as `sitescope_app`: not a superuser. `SET session_replication_role`, `CREATE TABLE` and reading `alembic_version` were refused. The role script, tested alone in a throwaway container, also refused `DROP TABLE` and `COPY ... PROGRAM`, and fails (exit 3) without a password |
| Backup and restore check (walkthrough) | `pg_dump -Fc`: 53 KB. Restored into a scratch database: 4 completed jobs, 27 pages, 26 indexed documents. The scratch database was then dropped |
| Search, statistics, broken links, graph (through the proxy) | "crawler": 6 results. Depths 0–3. One broken link (`/about.html` → `/missing.html`, 404). Graph: 15 nodes, 24 edges |
| Writes without a token | `POST /api/v1/crawls` and `.../cancel`: 403 `writes_disabled`, with or without a bearer token |
| Writes with `SITESCOPE_ADMIN_TOKEN` set | none or wrong: 401 with `WWW-Authenticate: Bearer`. Right: 201, then cancel 200. 8 quick guesses: 401, 401, then 429 × 6 |
| Internal endpoints | `/metrics`, `/docs`, `/redoc`, `/openapi.json`, `/health` and 8 traversal spellings (`/api/../metrics`, `/api/%2e%2e/metrics`, ...): 404, no metrics |
| Security headers | CSP, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, COOP and `Permissions-Policy` on pages and API responses |
| Strict CSP in a browser | every page at desktop and phone widths in Edge: no CSP violation, no page error. A probe confirmed violations are reported |
| Read rate limit | 60 quick requests: 41 × 200, 19 × 429 (`rate_limited` problem, `Retry-After: 1`). 200 again after 5 s |
| Crash | SIGKILL of a crawler process: restarted (restart count 1) and healthy again within 10 s |
| Graceful stop | `stop indexer` (2 replicas): 2.7 s, exit code 0, "Indexer stopped" logged |
| Memory at rest after the demo crawl (`docker stats`) | 601.6 MiB in total in the first run, 615.4 MiB in the walkthrough. Python roles 64–77 MiB each, PostgreSQL 101–115 MiB, nginx 15 and 4 MiB, Redis 7–8 MiB |

Not validated: a public host, a real domain with Let's Encrypt (the HTTPS overlay was checked as
configuration only), arm64 images, and sustained load through the proxy.
