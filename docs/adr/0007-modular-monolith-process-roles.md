# ADR-0007: Modular monolith with separate process roles

- Status: Accepted
- Date: 2026-11-06

## Context

SiteScope has four runtime roles: the API, the scheduler, the crawler worker, and the indexer worker.
They share models, configuration, and database logic.

- **Microservices** would add network APIs, versioning, and deployment overhead, with no benefit at this
  scale.
- **A single process** running everything could not demonstrate crash isolation or independent scaling.

## Decision

- **One codebase.** A single Python package, `backend/app`, organized in layered modules: `api`,
  `services`, `db`, `queue`, `scheduler`, `crawler`, `indexing`, `workers`.
- **One process per role.** Each role has its own entrypoint:

  | Role | Entrypoint |
  |---|---|
  | API | `uvicorn app.main:app` |
  | Scheduler | `python -m app.scheduler` |
  | Crawler worker | `python -m app.workers.crawler` |
  | Indexer worker | `python -m app.workers.indexer` |

- **One Docker image** for all roles.
- **No shared runtime state.** Roles communicate only through PostgreSQL and Redis.
- **Import rules:**
  - `api` never imports `workers` or `scheduler`;
  - `crawler` has no database imports;
  - `queue` has no domain logic.
- **Versioned messages.** Messages carry a `v` field, so a rolling restart can briefly run mixed
  versions.

## Consequences

- **Shared code:** typed models, one test suite, and one image.
- **Independent scaling:** each role scales on its own (`docker compose up --scale crawler-worker=N`),
  and killing one process is a realistic failure test.
- **Boundaries need policing:** a shared codebase can blur boundaries, so the import rules are checked
  in review and can later be enforced with a lint rule.
- **Single release:** all roles are released together.

## Alternatives considered

- **A microservice per role:** more operational work than value at this size.
- **A single process running every role:** the simplest to run, but it hides the failure modes this
  project exists to handle.
