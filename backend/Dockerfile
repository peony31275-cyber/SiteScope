# SiteScope backend image (Phase 11, ADR-0025). One image serves every Python role: the API,
# the scheduler, crawler workers, the index dispatcher, indexers, the migration step and the
# operator commands. deploy/compose.yaml gives each service its command.
#
#   docker build -t sitescope-backend:local backend
#
# The build context is an allowlist (.dockerignore): the application, the migrations and the
# locked runtime dependencies. Never the tests, a virtual environment or a .env file.

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie

# --- Build: the locked dependencies in a virtual environment, and byte-compiled sources ------
FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
# Exact versions for Linux, from requirements/lock.py. --no-deps installs the lock as it is,
# and pip check proves it complete.
COPY requirements/runtime.txt /tmp/runtime.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-deps --require-virtualenv -r /tmp/runtime.txt \
    && /opt/venv/bin/pip check \
    && /opt/venv/bin/pip uninstall --yes --quiet pip
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY app /app/app
# Compiled now: the runtime filesystem is read-only, and the processes start faster.
RUN /opt/venv/bin/python -m compileall -q -j 0 /app/app /app/alembic

# --- Runtime -------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="sitescope-backend" \
      org.opencontainers.image.description="SiteScope API, scheduler, crawler workers, index dispatcher and indexers" \
      org.opencontainers.image.licenses="MIT"
# The processes run as an unprivileged user that owns nothing in the image: code and
# dependencies are root's, read-only to it.
RUN groupadd --gid 10001 sitescope \
    && useradd --uid 10001 --gid sitescope --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin sitescope
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SITESCOPE_ENVIRONMENT=production
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
WORKDIR /app
USER 10001:10001
EXPOSE 8000
# The API by default; the compose file names each role's command. SIGTERM stops every role
# gracefully: uvicorn drains its requests, workers finish or hand back their tasks.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-server-header", "--timeout-graceful-shutdown", "10"]
