# Third-party software

SiteScope's own code is under the MIT license ([LICENSE](LICENSE)). It depends on the software
below, under its own licenses. This file summarizes them; the license texts ship with each
package and image.

## Libraries

These are installed with the application or bundled into it. Checked on 2026-11-08 from the
packages' metadata.

- **Backend runtime** (`backend/requirements/runtime.txt`, 32 packages): MIT, BSD-3-Clause,
  Apache-2.0 and PSF-2.0. Among them are FastAPI, Starlette, Pydantic, SQLAlchemy, asyncpg,
  Alembic, redis-py, uvicorn, uvloop (MIT or Apache-2.0), httpcore2 and Beautiful Soup.
- **Dashboard bundle** (production dependencies of `frontend/package.json`, 50 packages with
  their dependencies): MIT, ISC and BSD-3-Clause. Among them are React, React Router, TanStack
  Query, Recharts, the d3 modules (bundled as victory-vendor) and Lucide icons.
- **Development and test tools** (pytest, ruff, mypy, Vite, Vitest, ESLint, Playwright, ...):
  used to build and test, not distributed with the application.

All of these permit use, modification and redistribution under MIT-licensed code, with their
notices kept.

## Services

These run as separate, unmodified programs from their official container images. SiteScope's
code does not include, link or modify them.

| Software | Image | License |
|---|---|---|
| PostgreSQL 18.6 | `postgres:18.6-trixie` | PostgreSQL License (permissive) |
| Redis 8.10.3 | `redis:8.10.3-alpine3.23` | Redis 8 is available under AGPLv3, RSALv2 or SSPLv1, at the user's choice |
| nginx 1.30.5 | `nginx:1.30.5-alpine` | BSD-2-Clause |
| Caddy 2.10.2 (HTTPS overlay only) | `caddy:2.10.2-alpine` | Apache-2.0 |
| Python 3.13.16 | `python:3.13.16-slim-trixie` | PSF License, on Debian packages under their own licenses |
| Node.js 24.14.0 (build stage only) | `node:24.14.0-trixie-slim` | MIT, plus bundled third-party licenses |

**Redis.** SiteScope talks to Redis over the network with the MIT-licensed redis-py client, and
runs the official image unmodified. Under any of Redis' three licenses, that leaves SiteScope's
code under MIT.

The obligations bind whoever modifies Redis or offers it as a service:

- **AGPLv3:** modified Redis code must be offered, as source, to the network users of the
  modified server.
- **RSALv2 and SSPLv1:** these restrict offering Redis itself as a managed service.

A deployment that must avoid these licenses can try Valkey (BSD-3-Clause), a fork of Redis 7.2.
SiteScope has not been tested with it.
