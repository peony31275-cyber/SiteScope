@echo off
rem SiteScope local demo (Windows): the whole stack on a disposable database, and the
rem dashboard on http://127.0.0.1:4173. It keeps WSL and Docker running while it runs, starts
rem PostgreSQL and Redis if needed, and cleans up when it stops. The development data is never
rem touched (backend\tests\dashboard_stack.py, docs\development.md "Local demo").
rem
rem   demo.cmd              start; open the printed URL; Ctrl+C stops it
rem   demo.cmd --stop       stop a demo running in another terminal
rem   demo.cmd --help       every option
setlocal
cd /d "%~dp0backend" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
  echo backend\.venv is missing: create it as docs\development.md, "Python environment", says.
  exit /b 1
)
".venv\Scripts\python.exe" -m tests.dashboard_stack %*
