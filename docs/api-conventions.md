# API Conventions

These conventions apply to every SiteScope HTTP endpoint. They were established in Phase 1. The
code lives in `backend/app/core/` and `backend/app/api/`. When a later phase introduces a new
pattern (for example, pagination), it adds that pattern here.

## URLs and versioning

- **Unversioned operational endpoints:** `GET /` (service index) and `GET /health` (liveness probe).
- **Everything else** lives under `/api/v1`.
- **Breaking changes** get a new version prefix. Additive changes do not: new endpoints and new
  optional response fields stay in the current version.

## Requests and responses

- Request and response bodies are JSON, with `snake_case` field names.
- Timestamps are UTC in ISO 8601 / RFC 3339 format with a `Z` suffix, for example
  `2026-11-06T21:13:07.788Z`.
- Every endpoint declares a Pydantic response model, so its schema appears in the OpenAPI
  document.
- Each OpenAPI `operationId` is the handler's function name (for example `get_liveness`). These
  names must be unique, so generated clients (such as the Phase 8 dashboard) get stable method
  names.

## Request IDs

- Every response carries an `X-Request-ID` header.
- A client may send its own `X-Request-ID` of 1–128 characters from `A–Z a–z 0–9 . _ -`, and the
  server reuses it. If the header is missing or contains anything else, the server generates a
  32-character hex ID.
- The same ID appears in every log line written while handling the request, and in the body of
  every error response.

## Errors: RFC 9457 problem details

Every error response has the media type `application/problem+json`. For example:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "Crawl job 42 does not exist.",
  "instance": "/api/v1/jobs/42",
  "code": "not_found",
  "request_id": "1d37623ac12c4f08aa4b1109568bfdde"
}
```

| Member | Meaning |
|---|---|
| `type` | Always `about:blank`. The problem kind is identified by `status` and `code`. |
| `title` | The HTTP status phrase. |
| `status` | The HTTP status code. |
| `detail` | Explanation specific to this occurrence. Omitted when it would only repeat `title`. |
| `instance` | The request path, without the query string. |
| `code` | Stable, machine-readable error code. Clients branch on this. |
| `request_id` | Same value as the `X-Request-ID` header. Quote it when reporting a problem. |
| `errors` | Present only for `validation_failed`. Each entry has `loc`, `msg`, and `type`. |

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request` | A generic domain error (`DomainError`) |
| 404 | `not_found` | An unknown route, or a `NotFoundError` from the service layer |
| 405 | `method_not_allowed` | Wrong method. The `Allow` header lists the permitted methods. |
| 409 | `conflict` | A `ConflictError` from the service layer |
| 422 | `validation_failed` | The request failed validation |
| 500 | `internal_error` | An unexpected error. Details appear only in the server logs, never in the response. |
| 503 | `dependency_unavailable` | PostgreSQL or Redis is unreachable, or rejected the connection. Sent with `Retry-After: 5`. Ordinary SQL errors are never reported this way. |
| other | status name in `snake_case` | For example, 403 → `forbidden` |

Rules for code that produces errors:

- Services raise `DomainError` subclasses (`app/core/errors.py`) and never build HTTP responses.
- Unexpected exceptions are logged once, with the request ID and traceback. The client receives
  only the generic 500 problem.

## Health endpoints

| Endpoint | Purpose | Checks dependencies? |
|---|---|---|
| `GET /health` | Liveness probe for load balancers and container health checks | No |
| `GET /api/v1/health/live` | Liveness with service name, version, and uptime | No |
| `GET /api/v1/health/ready` | Readiness: runs `SELECT 1` on PostgreSQL and `PING` on Redis, concurrently, each within `SITESCOPE_READINESS_TIMEOUT_SECONDS` (default 2 s) | Yes |

Liveness means only that the process is serving requests. It never reports anything about
PostgreSQL or Redis. An outage of either therefore never makes an orchestrator restart a healthy
API process.

Readiness answers `200` when both dependencies respond, and `503` otherwise. It is the one
endpoint that answers `503` with its own schema instead of problem+json, so that probes and
dashboards parse a single shape:

```json
{
  "status": "not_ready",
  "checks": {
    "postgres": {"status": "ok", "latency_ms": 4.11},
    "redis": {"status": "unavailable", "latency_ms": 0.39}
  },
  "checked_at": "2026-11-06T23:53:41.147208Z"
}
```

- Each check's `status` is `ok`, `unavailable` (refused, rejected credentials, …) or `timeout`.
- The body never contains hosts, ports, database names, users, passwords or exception text. Those
  details go to the server log, which records only the exception type.
- Responses carry `Cache-Control: no-store`.

## Documentation endpoints

| Format | Path |
|---|---|
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI 3.1 JSON | `/openapi.json` |
