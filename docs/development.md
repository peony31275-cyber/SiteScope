# Local Development (Windows PowerShell)

These are the backend commands for the Windows 10 development machine. Each command was run in
Windows PowerShell 5.1 at the end of Phase 1. PowerShell 5.1 has no `&&`: enter commands one per
line, or chain them with `;`.

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | Use the interpreter path directly, or set the execution policy (see above). |
| `[Errno 10048]` or "address already in use" | Port 8000 is taken. Use `--port 8001`, or find the owning process: `Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| `ModuleNotFoundError: No module named 'app'` | Run from `backend\`, and re-run the install command. |
| Settings validation error at startup | Check `backend\.env` and any `SITESCOPE_` variables against `backend\.env.example`. |
