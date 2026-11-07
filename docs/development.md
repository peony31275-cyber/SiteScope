# Local Development (Windows PowerShell)

These are the development commands for the Windows 10 development machine:

- The backend commands were run in Windows PowerShell 5.1 at the end of Phase 1.
- The Docker commands were run during the Docker setup on 2026-11-06 (see [Docker (WSL 2)](#docker-wsl-2)).
- The database and Compose commands were run in Phase 2B.
- The crawl job commands were run in Phase 2C.

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

Jobs stay `queued`: nothing crawls them until the scheduler and the workers exist (Phases 3–5).

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

## Database migrations

The schema is managed only through Alembic. Never change it by hand in a database you want to
keep. Run the commands from `backend\`:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head      # apply all pending migrations
.\.venv\Scripts\python.exe -m alembic current           # revision of the database, e.g. "0002 (head)"
.\.venv\Scripts\python.exe -m alembic history           # the migration chain
.\.venv\Scripts\python.exe -m alembic upgrade head --sql    # print the SQL without connecting
```

To change the schema:

1. **Update the models.** Change `app/db/models.py`.
2. **Generate a draft.** It needs a database at head:
   ```powershell
   .\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe the change" --rev-id 0003
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
- **Redis.** Redis tests use logical database 15 and namespaced keys.
- **Crawl job tests** set their own approved hosts and limits, so they do not depend on the
  crawl settings in `backend\.env`.

To run them, make sure:

1. a WSL session is open;
2. the services are healthy (`wsl docker compose ps`);
3. `backend\.env` contains both passwords.

Then, from `backend\`:

```powershell
.\.venv\Scripts\python.exe -m pytest --require-services            # everything (unit + integration)
.\.venv\Scripts\python.exe -m pytest -m "not integration"          # unit tests only, no services needed
.\.venv\Scripts\python.exe -m pytest -m integration --require-services
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
| `alembic` says "Can't locate revision" | The database was migrated by a different branch of the code. Check `alembic current` against `alembic history`. |
