# Local Development (Windows PowerShell)

These are the development commands for the Windows 10 development machine:

- The backend commands were run in Windows PowerShell 5.1 at the end of Phase 1.
- The Docker commands were run during the Docker setup on 2026-11-06 (see [Docker (WSL 2)](#docker-wsl-2)).

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
```

To see response headers such as `X-Request-ID`, use `curl.exe`. In Windows PowerShell 5.1, plain
`curl` is an alias for `Invoke-WebRequest`, not curl.

```powershell
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i -H "X-Request-ID: my-trace-1" http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/does-not-exist    # 404 application/problem+json
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
2. **Run Docker commands.** Either use the Ubuntu terminal, where the repository is at
   `/mnt/c/Users/24419/Desktop/SITESCOPE`, or run `wsl docker ...` from PowerShell in the
   repository root. WSL translates the current directory, for example `C:\...\SITESCOPE` becomes
   `/mnt/c/.../SITESCOPE`.
   ```powershell
   Set-Location C:\Users\24419\Desktop\SITESCOPE
   wsl docker compose version          # verified
   wsl docker compose up -d            # once Phase 2 adds docker-compose.yml
   ```
   A one-off `wsl docker ...` command ends its own session immediately, so step 1 is still needed.
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
- **Planned endpoints (Phase 2, not implemented yet):** PostgreSQL at `127.0.0.1:5432` and Redis at
  `127.0.0.1:6379`. The backend will read the connection URLs from `SITESCOPE_` settings, whose
  exact names are defined in Phase 2.
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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | Use the interpreter path directly, or set the execution policy (see above). |
| `[Errno 10048]` or "address already in use" | Port 8000 is taken. Use `--port 8001`, or find the owning process: `Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| `ModuleNotFoundError: No module named 'app'` | Run from `backend\`, and re-run the install command. |
| Settings validation error at startup | Check `backend\.env` and any `SITESCOPE_` variables against `backend\.env.example`. |
