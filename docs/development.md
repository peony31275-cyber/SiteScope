# Local Development (Windows PowerShell)

These are the development commands for the Windows 10 development machine:

- The backend commands were run in Windows PowerShell 5.1 at the end of Phase 1.
- The Docker commands were run during the Docker setup on 2026-11-06 (see [Docker (WSL 2)](#docker-wsl-2)).
- The database and Compose commands were run in Phase 2B.
- The crawl job commands were run in Phase 2C.
- The safe fetcher commands were run in Phase 3.
- The crawl commands were run in Phase 4, against a separate database (see
  [Crawling](#crawling-phase-4)).
- The dashboard commands were run in Phase 9 (see [Dashboard](#dashboard-phase-9)).

PowerShell 5.1 has no `&&`: enter commands one per line, or chain them with `;`.

Run everything from the `backend` directory:

```powershell
Set-Location C:\Users\24419\Desktop\SITESCOPE\backend
```

## Python environment

The virtual environment at `backend\.venv` (Python 3.13) already exists. There are two ways to
use it:

- **Call its interpreter directly.** This works without any setup, and the rest of this document
  uses it:
  ```powershell
  .\.venv\Scripts\python.exe -m <tool>
  ```
- **Activate it.** After activation, plain `python -m <tool>` uses the virtual environment:
  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```
  On a default Windows 10 installation, PowerShell refuses to run scripts and reports "running
  scripts is disabled on this system". To allow local scripts for your user account, run this once:
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```

## Install dependencies

This installs the backend in editable mode, together with the development tools declared in
`backend/pyproject.toml`. Run it again whenever `pyproject.toml` changes.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Configuration

Settings come from environment variables with the `SITESCOPE_` prefix. For local development, you
can also put them in `backend\.env`. That file is git-ignored and must never be committed.

```powershell
Copy-Item .env.example .env            # then edit .env
$env:SITESCOPE_LOG_LEVEL = "DEBUG"     # or override a value for the current session only
```

`backend\.env.example` documents every supported variable. The application refuses to start if a
value is invalid, or if `.env` contains any key that is not a known `SITESCOPE_` setting. This
includes misspelled settings and variables meant for other tools.

## Run the API

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

- The server listens on <http://127.0.0.1:8000>, which is reachable only from this machine.
- `--reload` restarts the server when code changes. Use it for development only.
- Stop the server with **Ctrl+C**. Shutdown is graceful and logs an `app.shutdown` event.
- Logs are JSON lines on stdout, one access line per request, each carrying a `request_id`.

API documentation:

| Format | URL |
|---|---|
| Swagger UI | <http://127.0.0.1:8000/docs> |
| ReDoc | <http://127.0.0.1:8000/redoc> |
| OpenAPI JSON | <http://127.0.0.1:8000/openapi.json> |

## Check the endpoints

While the server is running, use a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/live
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready    # needs PostgreSQL and Redis
```

To see response headers such as `X-Request-ID`, use `curl.exe`. In Windows PowerShell 5.1, plain
`curl` is an alias for `Invoke-WebRequest`, not curl.

```powershell
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i -H "X-Request-ID: my-trace-1" http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/does-not-exist    # 404 application/problem+json
```

### Crawl jobs

No crawl target is approved by default (see [Crawl targets and limits](#crawl-targets-and-limits)),
so approve one for the server process first. The example hosts belong to `example.com` and
`example.org`, which are reserved for documentation (RFC 2606) and serve no real site. Start the
server in a window where the variable is set:

```powershell
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "docs.example.com,*.example.org"   # this window only
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

Then, in a second window (from `backend\`):

```powershell
$base = "http://127.0.0.1:8000/api/v1/crawls"
$body = '{"seed_url": "https://docs.example.com/guide/", "max_depth": 3, "max_pages": 500}'
$job = Invoke-RestMethod -Method Post -Uri $base -ContentType "application/json" -Body $body
$job                                                       # "queued", normalized seed_url
Invoke-RestMethod "$base/$($job.id)" | ConvertTo-Json -Depth 5   # the job and its progress
Invoke-RestMethod ($base + "?limit=20")                    # newest first, with next_cursor
```

`Invoke-RestMethod` throws on an error status. The problem+json body is in
`$_.ErrorDetails.Message`:

```powershell
$bad = '{"seed_url": "https://www.google.com/", "max_depth": 1, "max_pages": 10}'
try { Invoke-RestMethod -Method Post -Uri $base -ContentType "application/json" -Body $bad } catch { $_.ErrorDetails.Message }
```

The rows are in PostgreSQL:

```powershell
wsl docker compose exec -T postgres psql -U sitescope -d sitescope -c "SELECT id, status, seed_url, admitted_count FROM crawl_jobs ORDER BY created_at DESC LIMIT 5"
```

A job stays `queued` until a scheduler starts it ([Distributed crawling](#distributed-crawling-phase-5)).

**Idempotent creation** (Phase 6). Send an `Idempotency-Key` header (1 to 255 visible ASCII
characters, for example a UUID) to make a retried request safe: the same key with an equivalent
body returns the job the first request created, with status `200` and `Idempotent-Replayed: true`
instead of `201`; the same key with a different body is a `409` (`idempotency_key_reused`). Keys
are kept for `SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS` (24 hours by default).

```powershell
$key = [guid]::NewGuid().ToString()
$headers = @{ "Idempotency-Key" = $key }
$first = Invoke-WebRequest -Method Post -Uri $base -ContentType "application/json" -Body $body -Headers $headers
$again = Invoke-WebRequest -Method Post -Uri $base -ContentType "application/json" -Body $body -Headers $headers
$first.StatusCode, $again.StatusCode, $again.Headers["Idempotent-Replayed"]   # 201, 200, true
```

**Cancel a job** (Phase 6). A queued job is cancelled at once (`200`); a running one stops: its
waiting URLs are cancelled at once, and the job is `cancelling` (`202`) until the URLs in flight
are final, then `cancelled`. Repeating the request changes nothing.

```powershell
Invoke-RestMethod -Method Post -Uri "$base/$($job.id)/cancel"
```

## Tests, lint, format, and type check

All four checks must pass before a change counts as done:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
```

Helpful variants:

```powershell
.\.venv\Scripts\python.exe -m ruff format .                        # apply formatting
.\.venv\Scripts\python.exe -m ruff check --fix .                   # apply safe lint fixes
.\.venv\Scripts\python.exe -m pytest tests\unit\api -v             # run one directory, verbose
.\.venv\Scripts\python.exe -m pytest -k request_id                 # run tests matching a name
```

pytest treats warnings as errors, so deprecations surface immediately.

The dashboard has its own checks (`npm run check` in `frontend\`; section
[Dashboard](#dashboard-phase-9)).

## Docker (WSL 2)

PostgreSQL and Redis run as Docker containers. Docker Engine runs **inside the WSL 2 Ubuntu
distribution**; this machine does not use Docker Desktop. The FastAPI backend keeps running on
Windows from `backend\.venv` and reaches the containers through `127.0.0.1`.

### What is installed

Set up and verified on 2026-11-06.

| Component | Version / setting |
|---|---|
| Windows | Windows 10 Home 22H2, build 19045.6466 |
| WSL | 3.0.3, kernel 6.18.54.1 |
| Distribution | `Ubuntu` (WSL 2): Ubuntu 24.04.3 LTS, systemd enabled (`/etc/wsl.conf`) |
| Docker Engine | 29.9.3 (`docker-ce`), containerd 2.4.2 |
| Docker Compose | v5.7.0 (`docker-compose-plugin`, the official plugin: `docker compose`) |
| Docker Buildx | 0.38.1 |
| Daemon config | `/etc/docker/daemon.json` sets `{"log-driver": "local"}` |
| Storage, cgroups | `overlayfs`, systemd cgroup driver, cgroup v2 |
| Non-root access | User `ziqiliu` is in the `docker` group |

Notes on the settings:

- **Log driver.** Docker's default `json-file` driver never rotates logs. The `local` driver
  rotates them, so container logs cannot fill the disk.
- **Docker group.** Membership is root-equivalent inside this distribution. It was approved for this
  single-user development machine.
- **No Docker Desktop.** Do not install Docker Desktop on this machine. Its WSL integration would
  conflict with the Docker Engine already installed in this distribution.

### Installation method

Docker Engine was installed from **Docker's official apt repository**, following
<https://docs.docker.com/engine/install/ubuntu/> and
<https://docs.docker.com/engine/install/linux-postinstall/>. Two common alternatives were
deliberately avoided:

- the convenience script (`get.docker.com`), because it installs without confirmation and can
  trigger unexpected major-version upgrades;
- Ubuntu's own `docker.io` packages.

The setup ran these steps as root, using `wsl -d Ubuntu -u root`. In an interactive Ubuntu shell,
the equivalent commands are:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
# The key's fingerprint was checked: 9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Post-install configuration
printf '{\n  "log-driver": "local"\n}\n' | sudo tee /etc/docker/daemon.json
sudo dockerd --validate --config-file /etc/docker/daemon.json
sudo systemctl restart docker
sudo usermod -aG docker "$USER"   # takes effect in new WSL sessions
```

Upgrading later uses the same repository:

```bash
sudo apt-get update
sudo apt-get install --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### How Docker starts and stops (read this first)

Docker runs only while the Ubuntu distribution runs. Three behaviours follow from that, all observed
on this machine:

- **Docker starts by itself.** When Ubuntu starts (any `wsl` command, or opening an Ubuntu
  terminal), systemd starts Docker automatically.
- **Containers can restart by themselves.** Containers with the restart policy `unless-stopped` come
  back automatically. A test container was reachable from Windows about 4 s after Ubuntu started.
- **Docker stops when WSL idles out.** WSL stops Ubuntu about 15–30 seconds after the last WSL
  session closes, even while containers are running. Docker and every container stop with it, and
  Windows can no longer reach them. On Windows 10, WSL offers no setting to change this.

So **keep one WSL session open while you develop.**

### Daily workflow

1. **Keep a WSL session open.** Open an Ubuntu terminal and leave it open: a Windows Terminal tab,
   or in VS Code the terminal **+** menu → **Ubuntu (WSL)**. If you prefer a dedicated keep-alive
   window instead, run this in PowerShell and press Ctrl+C when you are done:
   ```powershell
   wsl -d Ubuntu --exec sleep infinity
   ```
2. **Start PostgreSQL and Redis.** The Compose file is `backend/compose.yaml`, so run Compose from
   `backend\`. In PowerShell, `wsl docker ...` runs Docker in Ubuntu, and WSL translates the current
   directory (`C:\...\backend` becomes `/mnt/c/.../backend`). In an Ubuntu terminal, `cd` to
   `/mnt/c/Users/24419/Desktop/SITESCOPE/backend` first.
   ```powershell
   Set-Location C:\Users\24419\Desktop\SITESCOPE\backend
   wsl docker compose up -d --wait     # waits until both health checks pass
   ```
   A one-off `wsl docker ...` command ends its own session immediately, so step 1 is still needed.
   See [Local services](#local-services-postgresql-and-redis) for details.
3. **Run the API on Windows** as described in [Run the API](#run-the-api).
4. **Finish.** Run `docker compose stop` (keeps data) or `docker compose down` (removes the
   containers but keeps named volumes), then close the WSL terminal.

### How the Windows-hosted API reaches PostgreSQL and Redis

- **Port publishing.** Containers publish their ports on `127.0.0.1` inside WSL, for example
  `127.0.0.1:5432:5432`. WSL's localhost forwarding makes those ports reachable from Windows. A test
  container published this way was verified to be:
  - reachable from Windows at `127.0.0.1` (TCP connect, and HTTP 200);
  - reachable from Windows at `localhost` (HTTP 200);
  - **not** reachable through the WSL VM's own network address.
- **Endpoints (Phase 2B):** PostgreSQL at `127.0.0.1:5432` and Redis at `127.0.0.1:6379`, set by
  the `SITESCOPE_POSTGRES_*` and `SITESCOPE_REDIS_*` settings (see
  [Connection configuration](#connection-configuration)). Verified end to end: the Windows-hosted
  API reports ready against both containers.
- **Use `127.0.0.1` in connection URLs.** `localhost` also worked in the test, but an explicit IPv4
  address does not depend on IPv6 (`::1`) forwarding.
- **Keep the 127.0.0.1 binding.** Docker's documentation warns that published ports bypass ufw and
  firewalld rules, so binding to `127.0.0.1` is the safe default. Never expose the Docker daemon
  socket over TCP.
- **Use named volumes for data.** Container data lives in Docker named volumes on WSL's ext4 disk.
  Bind mounts from `C:` (`/mnt/c/...`) are slow and are not used for database data.

### Verify Docker

Run these in an Ubuntu terminal, or prefix each with `wsl` in PowerShell:

```bash
docker --version                 # Docker version 29.9.3, build 5457199
docker compose version           # Docker Compose version v5.7.0
docker info                      # Logging Driver: local; Cgroup Driver: systemd; no warnings
docker run --rm hello-world      # "Hello from Docker!"
```

### Docker troubleshooting

| Symptom | What to check or run |
|---|---|
| Windows cannot reach a container port | Is Ubuntu running? `wsl --list --running`. Is the container up with its port? `docker ps`. Is the port reachable? `Test-NetConnection 127.0.0.1 -Port 5432` (PowerShell). |
| Containers stopped unexpectedly | WSL stopped Ubuntu after the last session closed. Keep a WSL session open; services with `restart: unless-stopped` return when Ubuntu starts. |
| `Cannot connect to the Docker daemon at unix:///var/run/docker.sock` | `systemctl status docker`, `sudo systemctl start docker`, `journalctl -u docker -n 50 --no-pager` |
| `permission denied ... docker.sock` | `id` must list `docker`. Open a new WSL session, or run `wsl --terminate Ubuntu`. Re-add with `sudo usermod -aG docker $USER` if needed. |
| Container logs | `docker logs <container>` or `docker compose logs -f` |
| Disk usage | `docker system df`. `docker system prune` removes stopped containers, unused networks, and dangling images. Add `--volumes` only if you mean to delete data. |
| Change daemon settings | Edit `/etc/docker/daemon.json`, then `sudo dockerd --validate --config-file /etc/docker/daemon.json` and `sudo systemctl restart docker` |
| Restart WSL | `wsl --terminate Ubuntu` restarts only Ubuntu. `wsl --shutdown` stops **all** distributions. |
| Admin command without the sudo password | From PowerShell: `wsl -d Ubuntu -u root -- <command>` |

## Local services: PostgreSQL and Redis

`backend/compose.yaml` defines the development infrastructure (Compose project `sitescope`):

| Service | Image | Port (host side) | Data | Health check |
|---|---|---|---|---|
| `postgres` | `postgres:18.6-trixie` | `127.0.0.1:5432` | volume `sitescope_postgres-data` | `pg_isready` over TCP |
| `redis` | `redis:8.10.3-alpine3.23` | `127.0.0.1:6379` | volume `sitescope_redis-data` (AOF, fsync every second) | `redis-cli ping` |

- Both services use `restart: unless-stopped`, and share the network `sitescope-backend`.
- The ports are bound to `127.0.0.1` only, so they are never reachable from the network.
- Redis requires a password. PostgreSQL uses SCRAM-SHA-256.
- Credentials come from `backend/.env`. Both Compose and the backend read that file.

**First start**

1. Create `backend\.env` from `backend\.env.example` and replace the placeholder passwords.
2. Keep a WSL session open (see [How Docker starts and stops](#how-docker-starts-and-stops-read-this-first)).
3. Run, from `backend\`:

```powershell
wsl docker compose up -d --wait                       # create and start both services
.\.venv\Scripts\python.exe -m alembic upgrade head    # create or upgrade the schema
```

PostgreSQL applies `SITESCOPE_POSTGRES_USER` and `SITESCOPE_POSTGRES_PASSWORD` only when it first
creates its data volume. To change them later, either `ALTER ROLE` inside psql, or reset the
development database (below).

**Everyday commands** (from `backend\`)

```powershell
wsl docker compose ps                                  # status and health
wsl docker compose logs -f postgres                    # follow a service's logs (Ctrl+C to stop)
wsl docker compose exec postgres psql -U sitescope -d sitescope   # interactive SQL shell
wsl docker compose exec postgres psql -U sitescope -d sitescope -c "SELECT count(*) FROM crawl_jobs"
wsl docker compose exec redis redis-cli ping           # authenticates through REDISCLI_AUTH
```

Type psql meta-commands such as `\dt` inside the interactive shell. Passed as a `-c` argument from
PowerShell, they lose their backslash on the way into WSL.

**Stopping, removing, deleting: three different commands**

| Command | Containers | Network | Named volumes (all data) | Use it when |
|---|---|---|---|---|
| `wsl docker compose stop` | stopped, kept | kept | **kept** | pausing work; `start` or `up -d` resumes |
| `wsl docker compose down` | removed | removed | **kept** | cleaning up containers; `up -d` recreates them with the same data |
| `wsl docker compose down -v` | removed | removed | **DELETED** | you deliberately want to lose all PostgreSQL and Redis data |

Ordinary restarts never lose data: `stop`/`start`, `down`/`up`, WSL shutting Ubuntu down, and
rebooting Windows all keep the volumes. Run `down -v` only on purpose.

## Connection configuration

All settings use the `SITESCOPE_` prefix. They are read from the environment or from
`backend\.env`, and the complete list with comments is in `backend\.env.example`.

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_POSTGRES_HOST` / `_PORT` | `127.0.0.1` / `5432` | where PostgreSQL listens (the container's published port) |
| `SITESCOPE_POSTGRES_USER` / `_DB` | `sitescope` / `sitescope` | role and database; also used by Compose to initialize the container |
| `SITESCOPE_POSTGRES_PASSWORD` | none | required for any connection; mandatory in production |
| `SITESCOPE_DB_POOL_SIZE` / `_MAX_OVERFLOW` | `5` / `5` | connections kept open per process / extra connections under load |
| `SITESCOPE_DB_POOL_TIMEOUT_SECONDS` | `5` | wait for a free pooled connection before failing |
| `SITESCOPE_DB_CONNECT_TIMEOUT_SECONDS` / `_COMMAND_TIMEOUT_SECONDS` | `5` / `30` | connect timeout / per-statement client-side timeout |
| `SITESCOPE_REDIS_HOST` / `_PORT` / `_DB` | `127.0.0.1` / `6379` / `0` | where Redis listens, and the logical database |
| `SITESCOPE_REDIS_PASSWORD` | none | required for any connection; mandatory in production |
| `SITESCOPE_REDIS_SOCKET_TIMEOUT_SECONDS` | `5` | connect and reply timeout |
| `SITESCOPE_READINESS_TIMEOUT_SECONDS` | `2` | deadline for each check in `GET /api/v1/health/ready` |

- **Use `127.0.0.1`, not `localhost`.** Both work through WSL's forwarding, but the IPv4 address
  avoids depending on IPv6.
- **Credential handling.** Passwords are `SecretStr` values. They are masked in `repr()`, never
  logged, and never included in API responses. The startup log shows only host, port and database.
- **Production checks.** In production the application refuses to start if a password is missing
  or still a template placeholder (`change-me...`).
- **Pooling.** Each process holds one connection pool. Connections are checked before reuse
  (`pool_pre_ping`) and recycled after 30 minutes.
- **No automatic Redis retries.** The Redis client fails fast instead of retrying silently.

## Crawl targets and limits

No crawl target is approved by default. Until you list one, `POST /api/v1/crawls` rejects every
request with `target_not_allowed`. Set the targets in `backend\.env`, or as environment
variables for one PowerShell session (as in [Crawl jobs](#crawl-jobs)).

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_CRAWL_ALLOWED_HOSTS` | empty | approved hosts, comma-separated: `docs.example.com` approves exactly that host; `*.example.org` approves its subdomains, not `example.org` itself |
| `SITESCOPE_CRAWL_ALLOWED_SCHEMES` | `http,https` | schemes a crawl target may use |
| `SITESCOPE_CRAWL_ALLOWED_PORTS` | `80,443` | ports a crawl target may use: the explicit port, or the scheme's default |
| `SITESCOPE_CRAWL_MAX_DEPTH_LIMIT` | `5` | the largest `max_depth` a job may request (0–50) |
| `SITESCOPE_CRAWL_MAX_PAGES_LIMIT` | `1000` | the largest `max_pages` a job may request (1–100000) |

- Entries are normalized like URL hosts and matched as whole host names, never as substrings.
- `localhost`, names under `.localhost`, and IP addresses are for development and tests only.
  With `SITESCOPE_ENVIRONMENT=production` the application refuses to start if the list contains
  any of them.
- The checks look only at the URL. They are not an SSRF defense: the Phase 3 safe fetcher must
  validate every resolved address ([architecture.md](architecture.md), section 12).

## Safe fetcher and HTML parser

`app/crawler/fetcher.py` fetches approved URLs over connections pinned to validated addresses.
`app/crawler/html_parser.py` extracts the title, text, links and metadata. The crawl driver
([Crawling](#crawling-phase-4)) uses both. The design is in
[architecture.md](architecture.md) (sections 8 and 12) and
[ADR-0013](adr/0013-safe-fetcher-pinned-connections.md).

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_FETCH_CONNECT_TIMEOUT_SECONDS` | `10` | DNS resolution, TCP connect and TLS handshake |
| `SITESCOPE_FETCH_READ_TIMEOUT_SECONDS` | `10` | each read or write |
| `SITESCOPE_FETCH_DEADLINE_SECONDS` | `60` | the whole fetch, redirects included (stops slow-drip responses) |
| `SITESCOPE_FETCH_MAX_BODY_BYTES` | `2097152` | the largest body after removing gzip or deflate (at most 10 MiB); larger bodies are rejected |
| `SITESCOPE_FETCH_MAX_REDIRECTS` | `5` | redirects followed; every target is validated again |
| `SITESCOPE_FETCH_INTERNAL_ORIGINS` | empty | exact origins that may resolve to private addresses (and, outside production, loopback) |
| `SITESCOPE_PARSE_MAX_LINKS` / `_MAX_TEXT_CHARS` | `1000` / `1000000` | links and visible text kept per page |

**Fetch a page by hand.** `app.crawler.cli` fetches one URL with the production settings and
prints a JSON report of the fetch and the parsed page. The URL must be an approved crawl target.
A local server also needs an internal origin, because its address is a loopback address.

In one window, serve the HTML test fixtures, on 127.0.0.1 only:

```powershell
Set-Location C:\Users\24419\Desktop\SITESCOPE\backend\tests\fixtures\html
C:\Users\24419\Desktop\SITESCOPE\backend\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1
```

In a second window, from `backend\`:

```powershell
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "127.0.0.1,localhost"
$env:SITESCOPE_CRAWL_ALLOWED_PORTS = "8765"
$env:SITESCOPE_FETCH_INTERNAL_ORIGINS = "http://127.0.0.1:8765,http://localhost:8765"
.\.venv\Scripts\python.exe -m app.crawler.cli http://127.0.0.1:8765/article.html --links 3
.\.venv\Scripts\python.exe -m app.crawler.cli http://localhost:8765/malformed.html --links 2
$env:SITESCOPE_FETCH_INTERNAL_ORIGINS = ""     # now localhost is refused: blocked_destination
.\.venv\Scripts\python.exe -m app.crawler.cli http://localhost:8765/article.html
```

`localhost` resolves to `::1` and `127.0.0.1`. The server listens on IPv4 only, so the fetcher
falls back to the second validated address; on Windows the refused first attempt costs about 2 s.

A public site works the same way, for example the documentation domain `example.com` (this
contacts the internet):

```powershell
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "example.com"
$env:SITESCOPE_CRAWL_ALLOWED_PORTS = "80,443"
.\.venv\Scripts\python.exe -m app.crawler.cli https://example.com/ --links 3
```

The exit status is 0 for a successful fetch and 1 for a failure. Failures report a
`failure` kind (for example `blocked_destination`, `tls_error`, `too_large`) and whether a retry
could succeed.

`app.crawler.cli` is a diagnostic for one URL: it neither reads robots.txt nor waits for a
politeness slot. Crawls go through the crawl driver below, which does both.

## Crawling (Phase 4)

`app/workers/local_crawler.py` runs one crawl job to completion in a single process: level by
level, with claims, fenced results, robots.txt and per-host politeness. It is a development and
test tool; the distributed scheduler and workers are in the next section (Phase 5). It needs
PostgreSQL and Redis.
The design is in [architecture.md](architecture.md) (sections 8, 9 and 12.4-12.5),
[ADR-0014](adr/0014-crawl-state-transactions-and-redirects.md) and
[ADR-0015](adr/0015-robots-txt-and-per-host-politeness.md).

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_CRAWLER_NAME` | `SiteScopeBot` | sent as `SiteScopeBot/<version>` in User-Agent; matched against robots.txt and `<meta name="...">` |
| `SITESCOPE_CRAWL_CONCURRENCY` | `4` | URLs one crawl process (a worker, or the driver) processes at once |
| `SITESCOPE_CRAWL_LEASE_SECONDS` | `90` | lease of a claimed URL (database clock); at least the fetch deadline + the margin |
| `SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS` | `10` | a task is cut off this long before its lease ends |
| `SITESCOPE_CRAWL_MAX_ATTEMPTS` | `3` | claims of one URL, retries and crashed attempts included; the failure of the last one is final |
| `SITESCOPE_POLITENESS_INTERVAL_SECONDS` | `1` | minimum time between request starts to one host, across processes |
| `SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST` | `1` | requests in flight to one host |
| `SITESCOPE_POLITENESS_MAX_WAIT_SECONDS` | `30` | the longest wait for a slot before `rate_limited` |
| `SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS` | `15` | a slot's lease, renewed while its request runs; a crashed process's slot frees after at most this long (Phase 6) |
| `SITESCOPE_ROBOTS_CACHE_SECONDS` / `_RETRY_SECONDS` | `86400` / `300` | robots.txt cache time; cache time of an unreachable robots.txt |
| `SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS` | `10` | cap on a robots.txt `Crawl-delay` |
| `SITESCOPE_REDIS_KEY_PREFIX` | `sitescope` | prefix of every Redis key |

**Retries** (Phase 6, [ADR-0017](adr/0017-retries-dead-letters-cancellation-idempotency.md)). A
transient failure (timeouts, connection errors, temporary DNS failures, `rate_limited`, HTTP 429,
500, 502, 503, 504) returns its URL to `pending` until a backoff ends; the failure of the last
attempt is final. The driver and the workers use the same policy:

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_RETRY_BASE_DELAY_SECONDS` | `2` | attempt n waits a random time up to `base * 2^(n-1)` (full jitter) |
| `SITESCOPE_RETRY_MAX_DELAY_SECONDS` | `60` | the cap of that random wait |
| `SITESCOPE_RETRY_AFTER_MAX_SECONDS` | `300` | a longer `Retry-After` (429, 503) is not waited for: the failure is final |
| `SITESCOPE_RETRY_HTTP_STATUSES` | `429,500,502,503,504` | statuses retried: 5xx, 408, 425 and 429 only (never 404 or 410) |

**Crawl the demo site.** The commands below use a separate database, `sitescope_manual`, so
that the development database and its jobs are untouched; drop the variable to use the
development database. In one window, serve the demo site (from the repository root):

```powershell
Set-Location C:\Users\24419\Desktop\SITESCOPE
backend\.venv\Scripts\python.exe -m http.server 8080 --bind 127.0.0.1 --directory demo-site
```

In a second window, from `backend\`, prepare the database and approve the demo site:

```powershell
wsl docker compose exec postgres createdb -U sitescope sitescope_manual
$env:SITESCOPE_POSTGRES_DB = "sitescope_manual"
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "127.0.0.1"
$env:SITESCOPE_CRAWL_ALLOWED_PORTS = "8080"
$env:SITESCOPE_FETCH_INTERNAL_ORIGINS = "http://127.0.0.1:8080"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Start the API in that window (`.\.venv\Scripts\python.exe -m uvicorn app.main:app`), and
create a job from a third window:

```powershell
$body = @{ seed_url = "http://127.0.0.1:8080/"; max_depth = 3; max_pages = 50 } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/crawls -ContentType "application/json" -Body $body
```

Then run the job, in a window with the same environment variables as the API:

```powershell
.\.venv\Scripts\python.exe -m app.workers.local_crawler $job.id
```

It logs one JSON line per URL and prints a summary; the exit status is 0 if the job completed,
1 if it failed, and 2 if the job does not exist. With the default politeness settings (one
request per second) the demo site, 14 requests including robots.txt, took about 16 s in the
Phase 4 check. `GET /api/v1/crawls/{id}` shows the
same progress from PostgreSQL. Expected result: 14 URLs; 12 stored pages; `/private/notes.html`
skipped by robots.txt; `/missing.html` a 404; `/docs/legacy.html` never reached.

The driver runs exactly the job it is given, and activates it if it is still `queued`: do not
pass the ID of a job you do not mean to crawl. If it stops (Ctrl+C, or a PostgreSQL or Redis
error), run it again: URLs it had claimed are taken over once their leases expire (90 s by
default), and nothing is crawled twice except the URLs that were in flight.

Remove the separate database afterwards with
`wsl docker compose exec postgres dropdb -U sitescope --force sitescope_manual`.

**Redis keys.** Politeness state is under `sitescope:rate:{<host>}:*` and robots.txt results under
`sitescope:robots:<crawler name>:<origin>`; every key expires. To look at them:

```powershell
wsl docker compose exec redis redis-cli -a "<password from backend/.env>" --no-auth-warning --scan --pattern "sitescope:*"
```

## Distributed crawling (Phase 5)

The scheduler (`python -m app.scheduler`) and any number of crawler workers
(`python -m app.workers.crawler`) run as separate processes and share the work through the Redis
stream `<prefix>:crawl` (consumer group `crawlers`). The design is in
[architecture.md](architecture.md) (sections 3, 9 and 10) and
[ADR-0016](adr/0016-distributed-execution-redis-streams.md).

> **Never start the scheduler against the development database.** The scheduler starts every
> `queued` job of its database. The development database (`sitescope`) holds three queued jobs
> from Phase 2C that must not run. Always set `SITESCOPE_POSTGRES_DB` to a separate database in
> the windows where the scheduler runs, as below. The workers only process tasks the scheduler
> published, and `python -m app.scheduler.status` only reads.

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_STREAM_READ_BLOCK_MS` | `2000` | how long `XREADGROUP` waits for tasks (below the Redis socket timeout) |
| `SITESCOPE_STREAM_RECLAIM_INTERVAL_SECONDS` | `15` | how often a worker looks for abandoned tasks (`XAUTOCLAIM`) |
| `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` | `100` | how long a task must have been idle before another worker takes it (at least the lease; 120 until Phase 6) |
| `SITESCOPE_STREAM_RECLAIM_BATCH_SIZE` | `50` | tasks taken over per `XAUTOCLAIM` |
| `SITESCOPE_STREAM_MAX_DELIVERIES` | `10` | a task delivered more often without being settled, whose URL can no longer be claimed (its attempts are used up), is poison: the URL fails as `max_deliveries_exceeded` and the message is dead-lettered (Phase 6). Deliveries counted during a PostgreSQL outage fail nothing |
| `SITESCOPE_STREAM_RETENTION_SECONDS` | `3600` | acknowledged entries older than this are trimmed |
| `SITESCOPE_STREAM_CONSUMER_MAX_IDLE_SECONDS` | `3600` | consumers with nothing pending are removed after this idle time |
| `SITESCOPE_SCHEDULER_INTERVAL_SECONDS` | `0.5` | pause between scheduler passes |
| `SITESCOPE_SCHEDULER_DISPATCH_BATCH_SIZE` | `100` | tasks published per job and pass |
| `SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB` | `100` | tasks of one job queued or in progress |
| `SITESCOPE_SCHEDULER_MAX_OUTSTANDING` | `1000` | tasks queued or in progress over all jobs |
| `SITESCOPE_SCHEDULER_MAX_RUNNING_JOBS` | `10` | jobs running at once; the oldest queued start first |
| `SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS` | `300` | a task without progress this long is published again if its message is gone (600 until Phase 6) |
| `SITESCOPE_SHUTDOWN_GRACE_SECONDS` | `30` | how long a stopping worker lets running tasks finish |
| `SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS` | `2` | how often a worker checks whether the jobs of its running tasks are being cancelled (Phase 6) |
| `SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS` | `86400` | how long an `Idempotency-Key` is remembered; the scheduler forgets expired keys (Phase 6) |

The timing order is checked at startup: task deadline < lease <= reclaim min idle <= reconcile
after <= retention.

**Crawl the demo site with several workers.** Prepare the separate database `sitescope_manual`
and serve the demo site as in [Crawling (Phase 4)](#crawling-phase-4). Then open one window per
process, from `backend\`, each with the same environment:

```powershell
$env:SITESCOPE_POSTGRES_DB = "sitescope_manual"      # never the development database
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "127.0.0.1"
$env:SITESCOPE_CRAWL_ALLOWED_PORTS = "8080"
$env:SITESCOPE_FETCH_INTERNAL_ORIGINS = "http://127.0.0.1:8080"
```

```powershell
.\.venv\Scripts\python.exe -m app.scheduler                   # window 1: the scheduler
.\.venv\Scripts\python.exe -m app.workers.crawler             # windows 2 and 3: one worker each
.\.venv\Scripts\python.exe -m uvicorn app.main:app            # window 4: the API
```

Create the job through the API as in the Phase 4 example. The scheduler activates it and
publishes its tasks; the workers process them. Each process logs JSON lines. A worker logs
`worker.started` with its name (`crawler-<host>-<pid>-<random>`, also its lease owner), and one
`task.handled` line per message: `disposition` (`ack`, `keep`, `dead_letter`), `reason`
(`completed`, `already_finished`, `fenced`, `leased`, ...), `message_id`, `job_id`, `url_id`,
`lease_epoch`, `depth`, `outcome` and `duration_ms`.

Diagnostics (read-only; it changes nothing in PostgreSQL or Redis):

```powershell
.\.venv\Scripts\python.exe -m app.scheduler.status
```

It prints the jobs by status, the running jobs with their URL states, the live leases per worker,
expired leases, and the stream: length, dead letters, the group's pending count and lag, each
consumer's pending count and idle time, and the newest dead letters (`--dead-letters N`, 10 by
default; Phase 6). A dead letter shows its `kind` (`malformed`, `unknown_url`, `job_mismatch`,
`max_deliveries_exceeded`), `reason`, delivery count, consumer and the task's IDs, never a URL.

**Cancellation** (Phase 6). `POST /api/v1/crawls/{id}/cancel` (see [Crawl jobs](#crawl-jobs)) works
while the processes run: the workers stop the job's running tasks within about
`SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS` and record them `cancelled`, and the scheduler marks the job
`cancelled` once nothing is in flight. If a worker crashed holding one of its URLs, the job stays
`cancelling` until that lease expires (90 s by default).

**Stopping.** Ctrl+C in a process's window stops it gracefully: a worker stops reading and lets
its running tasks finish for up to `SITESCOPE_SHUTDOWN_GRACE_SECONDS`, then cancels the rest; a
second Ctrl+C cancels at once. Another program can send `CTRL_BREAK_EVENT` to a process started
with `CREATE_NEW_PROCESS_GROUP` (the tests do). Closing a window or killing a process is safe too:
its unfinished tasks keep their leases and pending messages, and other workers take them over
after `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` (100 s by default). A politeness slot it held
frees within `SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS` (15 s).

**Redis keys.** The stream `sitescope:crawl` with its group `crawlers`, and the dead-letter
stream `sitescope:crawl:dlq`:

```powershell
wsl docker compose exec redis redis-cli -a "<password from backend/.env>" --no-auth-warning XINFO GROUPS sitescope:crawl
wsl docker compose exec redis redis-cli -a "<password from backend/.env>" --no-auth-warning XINFO CONSUMERS sitescope:crawl crawlers
```

**Benchmark.** `benchmarks/distributed_crawl.py` measures crawl throughput with 1, 2 and 4 worker
processes against local servers, on its own disposable database; see
[benchmarks/README.md](../benchmarks/README.md).

## Search indexing (Phase 7)

Crawled pages are indexed asynchronously by two more kinds of process (design:
[architecture.md](architecture.md), sections 3, 10.8 and 13;
[ADR-0018](adr/0018-asynchronous-indexing-pipeline.md)):

- the **index dispatcher** (`python -m app.scheduler.index_dispatcher`) publishes the stored pages
  waiting to be indexed to the stream `<prefix>:index` (consumer group `indexers`), republishes
  tasks whose messages were lost, and marks the indexing of finished jobs complete. It starts no
  crawl job and changes no crawl state;
- **indexers** (`python -m app.workers.indexer [--concurrency N]`, as many as needed) build each
  page's weighted search vector inside PostgreSQL and mark it `indexed`.

Apply migration `0004` first (`alembic upgrade head`, see [Database migrations](#database-migrations)).

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_INDEX_CONCURRENCY` | `4` | index tasks one indexer runs at once (1 to 64); each holds one database connection, so at most `SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW` (10 by default; `--concurrency` too) |
| `SITESCOPE_INDEX_MAX_ATTEMPTS` | `3` | attempts per page, counted in PostgreSQL before each one starts (crashed attempts count) |
| `SITESCOPE_INDEX_MAX_BODY_CHARS` | `100000` | characters of a page body that are indexed (1000 to 150000); title, description and headings are indexed whole |
| `SITESCOPE_INDEX_TASK_DEADLINE_SECONDS` | `20` | an index attempt's deadline, for all its statements together (shorter than `SITESCOPE_DB_COMMAND_TIMEOUT_SECONDS`) |
| `SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS` | `60` | how long an index task must have been idle before another indexer takes it over (at least the deadline + 2 s) |
| `SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS` | `300` | a queued page without progress this long is published again if its message is gone |
| `SITESCOPE_INDEX_DISPATCH_BATCH_SIZE` | `200` | tasks published per dispatcher pass |
| `SITESCOPE_INDEX_MAX_OUTSTANDING` | `1000` | pages queued at most at any time |
| `SITESCOPE_INDEX_DISPATCHER_INTERVAL_SECONDS` | `0.5` | pause between dispatcher passes |
| `SITESCOPE_SEARCH_TIMEOUT_SECONDS` | `5` | a search is stopped after this long (shorter than `SITESCOPE_DB_COMMAND_TIMEOUT_SECONDS`) |

The timing order is checked at startup: index task deadline + 2 s <= reclaim min idle <= reconcile
after <= `SITESCOPE_STREAM_RETENTION_SECONDS`.

**Connections.** Each process opens at most `SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW`
(10 by default) PostgreSQL connections, and an indexer uses at most one per running task. Keep the
total over all processes (API, scheduler, crawler workers, index dispatchers, indexers) below
PostgreSQL's `max_connections` (100 in the development container).

**Demonstration: crawl, persist, publish, index, search.** Prepare the separate database
`sitescope_manual` and serve the demo site as in [Crawling (Phase 4)](#crawling-phase-4). In every
window below, from `backend\`, set the same environment as in
[Distributed crawling](#distributed-crawling-phase-5):

```powershell
$env:SITESCOPE_POSTGRES_DB = "sitescope_manual"      # never the development database
$env:SITESCOPE_CRAWL_ALLOWED_HOSTS = "127.0.0.1"
$env:SITESCOPE_CRAWL_ALLOWED_PORTS = "8080"
$env:SITESCOPE_FETCH_INTERNAL_ORIGINS = "http://127.0.0.1:8080"
```

1. **Start the processes**, one window each:

   ```powershell
   .\.venv\Scripts\python.exe -m app.scheduler                    # the crawl scheduler
   .\.venv\Scripts\python.exe -m app.workers.crawler              # a crawler worker (or two)
   .\.venv\Scripts\python.exe -m app.scheduler.index_dispatcher   # the index dispatcher
   .\.venv\Scripts\python.exe -m app.workers.indexer              # an indexer (or two)
   .\.venv\Scripts\python.exe -m uvicorn app.main:app             # the API
   ```

2. **Crawl.** Create a job through the API as in the Phase 4 example
   (`POST /api/v1/crawls` with `"seed_url": "http://127.0.0.1:8080/"`). The scheduler starts it and
   the crawler workers fetch its pages.
3. **Persist.** Each stored page gets a `pending` search document in the same transaction
   (`noindex` pages get none).
4. **Publish.** The index dispatcher's log shows `index.dispatched` and `index_dispatcher.pass`
   lines; the documents become `queued`.
5. **Index.** Each indexer logs one `task.handled` line per message (`reason` `indexed`, with
   `job_id`, `page_id`, `content_version`, `attempt`, `lexemes`, `duration_ms`). Once the job is
   finished and no document is pending or queued, the dispatcher logs `job.indexing_completed`.
   The job detail shows the progress:

   ```powershell
   (Invoke-RestMethod http://127.0.0.1:8000/api/v1/crawls/<job-id>).indexing
   ```

   `status` is `pending`, `running`, `indexed` (complete) or `failed` (complete, with pages that
   could not be indexed), with the counts per document status and `completed_at`.
6. **Search** (read-only; since Phase 8 also `GET /api/v1/search`, see
   [Search and website analysis](#search-and-website-analysis-phase-8)):

   ```powershell
   .\.venv\Scripts\python.exe -m app.scheduler.search <job-id> "robots"
   .\.venv\Scripts\python.exe -m app.scheduler.search <job-id> '"exact phrase" -excluded' --limit 5
   ```

   It prints JSON: the normalized query, `searchable` (false for a query of stop words or only
   excluded words, which is not run), `total`, and per result the URL, title, rank, a plain-text
   snippet with highlight ranges, and when the page was fetched and indexed, then the job's
   indexing progress. Only pages whose index reflects their current content are returned.

Diagnostics: `python -m app.scheduler.status` adds an `indexing` section (documents by status,
attempts running per indexer, the oldest pending and queued documents, finished jobs whose
indexing is not complete) and an `index_stream` section (length, pending messages, consumers,
newest dead letters: `malformed`, `unknown_document`, `job_mismatch`, `version_ahead`).

**Recovery.** Killing an indexer is safe: its attempt is already counted, its lease expires, and
another indexer takes its message over after `SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS`. A page
fails for good after `SITESCOPE_INDEX_MAX_ATTEMPTS` attempts that crashed or ran past their
deadline (`search_documents.last_error`, for example `max_attempts_exceeded (last error:
deadline_exceeded)`, and a `page.index_failed` event). Pages of a cancelled job are
still indexed (ADR-0018). Reindexing a job (`request_reindex` in
`app/services/search_index.py`) sets its indexed and failed documents back to `pending`; search
keeps serving the previous vectors until they are rebuilt.

**Language.** The index uses PostgreSQL's `english` configuration: English words are stemmed and
English stop words ignored. Chinese and other languages written without spaces are not split into
words, so a query matches such text only as a whole run of characters.

## Search and website analysis (Phase 8)

The API serves search and read-only analysis of each crawl job (design:
[ADR-0019](adr/0019-website-analysis-from-stored-crawl-results.md); request and response details:
[api-conventions.md](api-conventions.md), sections "Search" and "Website analysis"). They read
PostgreSQL only: no crawl target is ever contacted, and nothing is written.

Apply migration `0005` first (`alembic upgrade head`): it widens two indexes the analysis queries
use. The endpoints also work at revision `0004`, only slower on large jobs.

| Setting | Default | Meaning |
|---|---|---|
| `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS` | `10` | an analysis request is stopped after this long, with `503 analysis_timeout` (0 < value <= 60, shorter than `SITESCOPE_DB_COMMAND_TIMEOUT_SECONDS`) |
| `SITESCOPE_SEARCH_TIMEOUT_SECONDS` | `5` | a search is stopped after this long, with `503 search_timeout` (Phase 7) |

**Try it** on a crawled and indexed job (for example the demonstration of
[Search indexing](#search-indexing-phase-7), with the API running), from PowerShell:

```powershell
$job = "<job-id>"
$api = "http://127.0.0.1:8000/api/v1"
Invoke-RestMethod "$api/search?job_id=$job&q=robots&page_size=5" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$api/crawls/$job/stats" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$api/crawls/$job/pages?limit=20"
Invoke-RestMethod "$api/crawls/$job/links?classification=robots_blocked"
Invoke-RestMethod "$api/crawls/$job/broken-links"
Invoke-RestMethod "$api/crawls/$job/link-graph?max_nodes=100&max_edges=500" | ConvertTo-Json -Depth 4
```

With `curl.exe` (Windows 10 ships it), quote the URL: `curl.exe -s "http://127.0.0.1:8000/api/v1/crawls/<job-id>/stats"`.

- **Paging.** The listings return `next_cursor`; pass it back unchanged as `cursor` with the same
  filters: `Invoke-RestMethod "$api/crawls/$job/links?cursor=$($page.next_cursor)"`. Search pages
  with `page` and `page_size` (at most 50) up to result 1,000 plus one page.
- **Reading the link report.** `broken-links` lists only links whose target answered 404 or 410
  (after its redirects). A link the crawl never requested (external, `nofollow`, beyond
  `max_depth` or the page budget, disallowed by robots.txt, not crawled yet) or that got no
  response is never reported as broken or as working: `links?classification=<class>` lists each
  kind, and `stats.links.by_class` counts them.
- **Errors.** An unknown job is `404`; invalid parameters or cursors are `422` with the field in
  `errors[].loc`; a request over its time limit is `503` with `analysis_timeout` or
  `search_timeout`. Request logs record the path only, never the query string (search terms,
  cursors).

**Demo-site end to end.** `tests/fault/test_api_end_to_end.py` runs the whole pipeline with real
processes (scheduler, two crawler workers, the index dispatcher, two indexers) on the demo site
served on 127.0.0.1, creates the job through the API, and checks every Phase 8 endpoint against the
demo site's known structure. It uses the disposable test database:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/fault/test_api_end_to_end.py -p no:cacheprovider --require-services
```

**Benchmarks.** `benchmarks/analysis.py` measures every analysis endpoint on synthetic
1,000- and 10,000-page jobs (results: `benchmarks/results/phase8-analysis.md`).

## Dashboard (Phase 9)

The React dashboard lives in `frontend\` (its README has the architecture, the design system and
every command; design decisions: [ADR-0020](adr/0020-dashboard-same-origin-typed-client.md)). It
needs Node.js 22.12 or newer (Node.js 24 is installed) and talks to the API on its own origin:
the Vite development and preview servers proxy `/api` to the API, so the API needs no CORS.

**Run it** with the API running (section [Run the API](#run-the-api)), from
`C:\Users\24419\Desktop\SITESCOPE\frontend`:

```powershell
npm ci             # once, and whenever package-lock.json changes
npm run dev        # http://127.0.0.1:5173, /api proxied to http://127.0.0.1:8000
```

`SITESCOPE_API_URL` points the proxy elsewhere, for example
`$env:SITESCOPE_API_URL = "http://127.0.0.1:8010"`. The dashboard shows the jobs of the database
the API uses; for crawls to make progress, the scheduler, crawler workers, index dispatcher and
indexers must run against the same database (sections
[Distributed crawling](#distributed-crawling-phase-5) and
[Search indexing](#search-indexing-phase-7)). Never start the scheduler against the development
database.

The two endpoints Phase 9 added are read-only: `GET /api/v1/crawl-policy` (approved targets and
job limits, for the job form) and `GET /api/v1/overview` (counts over every job, in one
snapshot). Apply migration `0005` to the database first if it is still at `0004` (section
[Search and website analysis](#search-and-website-analysis-phase-8)).

**Checks**, all from `frontend\`:

```powershell
npm run check          # type check, lint, format check, tests, production build
npm test               # unit and component tests only (Vitest)
npm run build          # production build in dist\
npm run preview        # serve dist\ on http://127.0.0.1:4173, with the same /api proxy
```

**End to end with the real stack.** `tests/dashboard_stack.py` starts everything on a disposable
database and runs the Playwright suite in the installed Microsoft Edge (no browser download).
With PostgreSQL and Redis up (`wsl docker compose up -d --wait`, and a WSL window kept open),
from `backend\`:

```powershell
.\.venv\Scripts\python.exe -m tests.dashboard_stack --e2e     # run the suite, then stop
.\.venv\Scripts\python.exe -m tests.dashboard_stack           # serve the stack until Ctrl+C
```

It creates and migrates `<SITESCOPE_POSTGRES_DB>_test_ui` (refusing any non-`_test` name), uses
Redis database 15 under its own key prefix, serves the demo site on 127.0.0.1 and a slow site on
127.0.0.2, starts the API, the scheduler, two crawler workers, the index dispatcher, two indexers
and `vite preview` (after a production build; `--no-build` skips it), prints their addresses,
and at the end stops every process, deletes its Redis keys and drops the database (`--keep`
keeps it). Logs and `stack.json` are in a temporary directory it prints. Arguments after `--`
go to `playwright test`; `$env:E2E_SCREENSHOTS = "1"` adds screenshots of every page.

**API types.** After an API change, regenerate the dashboard's types:

```powershell
# in backend\
.\.venv\Scripts\python.exe -m app.api.openapi_export ..\frontend\src\lib\api\openapi.json
# in frontend\
npm run api:types
```

The backend unit test `tests/unit/api/test_openapi_snapshot.py` fails until the snapshot is
current.

## Observability and benchmarks (Phase 10)

**Metrics.** The API serves `GET /metrics` (Prometheus text format) next to `/health`, outside
`/api`: the dashboard's proxy never forwards it. With the API running:

```powershell
curl.exe -s http://127.0.0.1:8000/metrics | Select-String sitescope_crawl_urls
```

Every long-running process (API, scheduler, workers, index dispatcher, indexers) writes a heartbeat
to Redis every `SITESCOPE_HEARTBEAT_INTERVAL_SECONDS` (default 10); `/metrics` renders each live
process's own counters and histograms with its `role` and `process` labels. What each series
means: [observability.md](observability.md).

**Status and health assessment.** `python -m app.scheduler.status` now also lists the live
processes (`processes`) and answers the operator's questions (`assessment`): PostgreSQL, Redis,
the API, crawl progress, crawler workers, indexing, repeated failures, recovery. `--stall-seconds`
(default 120) sets how long work may wait before it is reported.

**Benchmarks.** Every script in `benchmarks/` creates its own disposable database
(`<SITESCOPE_POSTGRES_DB>_test_bench`), uses Redis database 15 under fresh key prefixes, serves its
sites on 127.0.0.x, and drops everything at the end; the outage scenarios of `recovery.py` start
disposable Redis and PostgreSQL containers in WSL Docker. Run one at a time (they share the
benchmark database), from `backend\`, with PostgreSQL and Redis up and nothing else heavy running.
The commands, their options and what each measures: [`benchmarks/README.md`](../benchmarks/README.md);
the results and their interpretation: [performance.md](performance.md).

**A new statement that joins a job's rows** (an analysis read, an admission-like write) runs
under `SET LOCAL enable_nestloop = off` (ADR-0023) and is measured with statistics that have not
seen the job: `benchmarks/admission_plans.py` builds that state (autovacuum off, a new job not
analyzed) and explains every analysis read with nested loops allowed and off.

To compare two versions of the code in one session, check the older one out with
`git worktree add --detach <dir> <commit>` and pass `--variant baseline=<dir>\backend
--variant current=.` to `crawl_scaling.py` (or `--variant` to the other scripts): runs of both
variants alternate, so a drift of the machine affects both. Remove the worktree afterwards with
`git worktree remove <dir>`.

Lint the scripts with the backend's configuration:

```powershell
.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml ..\benchmarks
.\.venv\Scripts\python.exe -m ruff format --config pyproject.toml --check ..\benchmarks
```

## Database migrations

The schema is managed only through Alembic. Never change it by hand in a database you want to
keep. Run the commands from `backend\`:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head      # apply all pending migrations
.\.venv\Scripts\python.exe -m alembic current           # revision of the database, e.g. "0005 (head)"
.\.venv\Scripts\python.exe -m alembic history           # the migration chain
.\.venv\Scripts\python.exe -m alembic upgrade head --sql    # print the SQL without connecting
```

To change the schema:

1. **Update the models.** Change `app/db/models.py`.
2. **Generate a draft.** It needs a database at head:
   ```powershell
   .\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe the change" --rev-id 0006
   ```
3. **Review and complete the draft.**
   - Autogenerate handles tables, columns, indexes, foreign keys and unique constraints.
   - CHECK constraints, triggers, functions and data migrations must be written or checked by hand.
   - Keep the migration self-contained: never import application code.
4. **Apply it and run the tests.** `test_migrated_schema_matches_the_models` fails if the
   migration and the models disagree.
5. **Never edit a migration once it has been applied anywhere that matters.** Add a new one instead.

Each migration runs in a single transaction, and PostgreSQL's DDL is transactional. A migration
that fails part-way therefore leaves no partial schema. A test interrupts a migration to prove this.

`alembic downgrade` exists, and the tests use it on disposable databases. Do not downgrade a
database whose data you need: dropping tables loses data.

## Development database reset

Resetting deletes all development data. Pick the smallest reset that solves the problem.

**Option 1: recreate only the PostgreSQL database.** Redis and the volume are kept. Run from
`backend\`:

```powershell
wsl docker compose exec postgres dropdb -U sitescope --force sitescope
wsl docker compose exec postgres createdb -U sitescope sitescope
.\.venv\Scripts\python.exe -m alembic upgrade head
```

`--force` disconnects open sessions first, so stop the API before running it. These commands were
verified on a scratch database; the development database itself was not dropped.

**Option 2: delete everything, including both volumes.** Use this after changing the PostgreSQL
credentials in `.env`, or when Redis data must go too.

```powershell
wsl docker compose down -v          # DELETES all PostgreSQL and Redis data
wsl docker compose up -d --wait
.\.venv\Scripts\python.exe -m alembic upgrade head
```

## Integration tests

Integration tests (`backend/tests/integration`, marker `integration`) run against the real
PostgreSQL and Redis containers. They never touch the development data:

- **Disposable PostgreSQL database.** At session start, the tests create `<SITESCOPE_POSTGRES_DB>_test`
  (`sitescope_test`), migrate it with Alembic (not `create_all`), and drop it at the end. Every
  test truncates the tables afterwards.
- **Migration tests.** These use a second disposable database, `sitescope_test_migrations`.
- **Redis.** Redis tests use logical database 15 and namespaced keys. Phase 4 tests give each
  test its own key prefix (`test-<random>`) and delete its keys afterwards.
- **Fetcher tests** (`tests/integration/test_fetcher.py`) start their own HTTP and TLS
  servers on 127.0.0.1, with a throwaway certificate authority from `trustme`. They need
  neither the containers nor the internet, and run even when the services are down.
- **Crawl job tests** set their own approved hosts and limits, so they do not depend on the
  crawl settings in `backend\.env`.
- **Crawl tests** (Phase 4): `test_crawl_state.py` runs the claim, result and level-advancement
  transactions, several of them concurrently; `test_politeness_redis.py` and
  `test_robots_policy.py` use real Redis; `test_local_crawler.py` crawls fixture sites and the
  demo site served on 127.0.0.1 (one test uses the standard library's static file server and
  the driver's command line). None of them contacts the internet.
- **Distributed tests** (Phase 5): `test_streams.py`, `test_task_dispatch.py`,
  `test_crawler_worker.py` and `test_scheduler.py` run in the test process.
  `test_distributed.py` and `backend/tests/fault/test_recovery.py` (marker `fault`, also marked
  `integration`) start real `python -m app.scheduler` and `python -m app.workers.crawler`
  processes (`tests/process_support.py`). They crawl local servers, stop processes with
  `CTRL_BREAK_EVENT`, kill or freeze them, and cut PostgreSQL or Redis off through a local TCP
  proxy. The child processes read `backend\.env` for credentials, but always get the test
  database and Redis database 15 explicitly: `child_environment` refuses any database that is not
  a disposable `*_test` one. Each process writes its JSON log to the test's `tmp_path` (pytest
  keeps the last three runs under `%TEMP%\pytest-of-<user>\`).
- **Retries, cancellation, idempotency** (Phase 6): `test_retries.py`, `test_cancellation.py` and
  `test_idempotency.py` run the transactions concurrently on separate connections;
  `backend/tests/fault/test_retries_and_cancellation.py` uses real processes and local servers
  whose pages fail on purpose (`LocalHttpServer(sequences=...)`).
- **Redis data loss** (Phase 6): `backend/tests/fault/test_redis_data_loss.py` sends `FLUSHALL`,
  but only to a disposable Redis container that it starts itself (`wsl -e docker run --rm ...
  redis:8.10.3-alpine3.23`, no persistence, a random password, 127.0.0.1 on a random port) and
  removes at the end. Before flushing it checks that the port and the server's `run_id` differ
  from the development Redis, and that a sentinel key in the development Redis survives.
  `FLUSHALL` is never sent to the development Redis: a separate database number does not isolate
  it. The test needs Docker in WSL; without it, the test is skipped (or fails with
  `--require-services`).

To run them, make sure:

1. a WSL session is open;
2. the services are healthy (`wsl docker compose ps`);
3. `backend\.env` contains both passwords.

Then, from `backend\`:

```powershell
.\.venv\Scripts\python.exe -m pytest --require-services            # everything (unit + integration)
.\.venv\Scripts\python.exe -m pytest -m "not integration"          # unit tests only, no services needed
.\.venv\Scripts\python.exe -m pytest -m integration --require-services
.\.venv\Scripts\python.exe -m pytest -m fault --require-services      # crash and outage tests only
```

Without `--require-services`, a missing service makes the integration tests **skip**, and the skip
reason says which service is unreachable and how to start it. With the flag they **fail** instead.
Use the flag whenever the services are supposed to be up.

Expect a few tests to take about 2 s each. They connect to a closed local port on purpose, and on
Windows a refused localhost connection takes about 2 s before it fails.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | Use the interpreter path directly, or set the execution policy (see above). |
| `[Errno 10048]` or "address already in use" | Port 8000 is taken. Use `--port 8001`, or find the owning process: `Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| `ModuleNotFoundError: No module named 'app'` | Run from `backend\`, and re-run the install command. |
| Settings validation error at startup | Check `backend\.env` and any `SITESCOPE_` variables against `backend\.env.example`. |
| `GET /api/v1/health/ready` returns 503 | Read `checks`. If a service is `unavailable` or `timeout`, check `wsl docker compose ps`, and make sure a WSL session is open. |
| `password authentication failed` after editing `.env` | PostgreSQL kept the password from the volume's first start. `ALTER ROLE ... PASSWORD`, or reset (Option 2). |
| Integration tests are all skipped | The skip reason names the unreachable service. Start it, or run with `--require-services` to make this an error. |
| `POST /api/v1/crawls` returns 422 `target_not_allowed`: "no crawl targets are approved on this server" | Set `SITESCOPE_CRAWL_ALLOWED_HOSTS` (see [Crawl targets and limits](#crawl-targets-and-limits)) and restart the server. |
| Startup fails with "production does not allow development-only crawl targets" | Remove `localhost`, `.localhost` names and IP addresses from `SITESCOPE_CRAWL_ALLOWED_HOSTS`, or run in `development`. |
| The fetcher reports `blocked_destination` for a local server | Add its exact origin to `SITESCOPE_FETCH_INTERNAL_ORIGINS` (development only); approving the host is not enough. |
| The fetcher reports `target_not_allowed` | Add the host to `SITESCOPE_CRAWL_ALLOWED_HOSTS`, and its port to `SITESCOPE_CRAWL_ALLOWED_PORTS` if it is not 80 or 443. |
| The scheduler started jobs you did not want to run | It starts every queued job of its database. Stop it (Ctrl+C), and always set `SITESCOPE_POSTGRES_DB` to a separate database before starting it (see [Distributed crawling](#distributed-crawling-phase-5)). |
| Tasks stay pending after a worker crashed | Expected for `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` (100 s by default); then a live worker takes them over. `python -m app.scheduler.status` shows the pending count per consumer. |
| A job stays `cancelling` | Its in-flight URLs are not final yet: a crashed worker's URL is cancelled when its lease expires, and only a running scheduler (or the single-process driver) finalizes the job. |
| `POST /api/v1/crawls` returns 409 `idempotency_key_reused` | The `Idempotency-Key` was already used for a different request (seed, depth or page limit). Use a new key for a new job. |
| A worker does not stop on Ctrl+C | It is finishing running tasks (up to `SITESCOPE_SHUTDOWN_GRACE_SECONDS`). Press Ctrl+C again to cancel them; they are recovered later. |
| `alembic` says "Can't locate revision" | The database was migrated by a different branch of the code. Check `alembic current` against `alembic history`. |
