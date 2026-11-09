# Security model

What SiteScope defends, against whom, how, and what remains. It covers the application and the
containerized deployment ([deployment.md](deployment.md)). Decisions:

- [ADR-0006](adr/0006-target-allowlist-ssrf-defenses.md), [ADR-0012](adr/0012-approved-crawl-targets.md)
  and [ADR-0013](adr/0013-safe-fetcher-pinned-connections.md): crawl scope and SSRF;
- [ADR-0020](adr/0020-dashboard-same-origin-typed-client.md): the dashboard;
- [ADR-0024](adr/0024-public-read-only-mode-and-administrator-writes.md): write access and abuse
  limits;
- [ADR-0025](adr/0025-container-packaging-and-deployment-topology.md): images and network.

## Assets and actors

**Assets**, most valuable first:

1. **The host and its neighbours.** A crawler is a machine that sends requests on someone's
   behalf. It must not become a way into private networks (cloud metadata, the database, the
   host's services), nor a tool to load third-party sites.
2. **Credentials:** the PostgreSQL and Redis passwords, and the optional admin token.
3. **The demo's availability**, and the integrity of its crawl results.
4. **The crawl results.** In the demo they are synthetic pages and public by design. In a
   private deployment they may be confidential.

**Actors:**

- the anonymous visitor of a public demo;
- the operator, with shell access to the host;
- the crawled websites, whose content is untrusted input;
- the supply chain: packages, base images, CI actions.

## Trust boundaries

```
internet ──► nginx (only published port; rate limits, headers) ──► API ──► PostgreSQL, Redis
                                                                     ▲
operator ── shell on the host ── docker compose exec ── operator commands (services)
crawler workers ──► safe fetcher ──► approved target only (demo-site; no internet route)
crawled HTML ──► parser (limits) ──► stored text ──► dashboard (text only, strict CSP)
```

## Threats and defenses

| Threat | Defenses | Evidence |
|---|---|---|
| A visitor starts or cancels crawls (resource abuse, tampering with the demo) | Writes refused in the backend: `403 writes_disabled` on a public deployment; `401` without the admin token where one is set (ADR-0024). The dashboard hides the form, as a convenience only. | `tests/unit/api/test_write_access.py`; deployment validation (writes 403 through the proxy, even with a bearer token) |
| Guessing the admin token | At least 32 characters in production, never a placeholder; constant-time comparison; refusals logged without the token; proxy limits writes to 10 a minute per address | `tests/unit/core/test_config.py`, `test_write_access.py`; validation (8 quick guesses: 401, 401, then 429) |
| Flooding the queue even with write access | At most `SITESCOPE_CRAWL_MAX_ACTIVE_JOBS` active jobs, exact under concurrency (advisory lock); per-job depth and page caps | `tests/integration/test_active_jobs_cap.py` (8 concurrent creations, cap 3: exactly 3) |
| SSRF: making the crawler reach internal services, cloud metadata, the database | Approved host patterns, schemes and ports (ADR-0012). The fetcher resolves DNS itself, validates every address, and pins the connection to it (no DNS rebinding window). Every redirect hop is validated again. Private and loopback addresses are refused except exact internal origins (the demo site). In the deployment, the crawlers' network has no route to the internet at all. | `tests/unit/crawler/test_destinations.py`, `test_scope.py`, `test_urls.py`, `tests/integration/test_fetcher.py`, `test_fetcher_policies.py`; validation (no egress from a crawler container) |
| Crawling arbitrary sites, or overloading one | Only approved targets (by default the bundled, synthetic demo site); robots.txt (RFC 9309) before every URL; per-host politeness through Redis (interval, concurrency); retries bounded with backoff; timeouts and an overall deadline per fetch | `test_robots.py`, `test_robots_policy.py`, `test_politeness.py`, `test_politeness_redis.py` |
| Hostile pages: huge, slow or compressed bodies, endless links | Body size cap after decompression; connect, read and total deadlines; per-page link and text caps; HTML parsing off the event loop | `tests/unit/crawler/test_fetcher.py`, `test_content.py`, `test_html_parser.py` |
| Script injection through crawled titles, snippets, anchors or URLs | Shown as text only: `dangerouslySetInnerHTML` is banned by lint; only http(s) URLs become links (`rel="noopener noreferrer nofollow"`). The proxy sends a strict CSP: `script-src 'self'`, `style-src 'self'`, `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`. | `frontend/eslint.config.js`; dashboard tests. Opt-in `e2e/screenshots.spec.ts`, run against the deployed stack on 2026-11-08: every page loads under the CSP without a violation, and a probe confirmed violations are reported. In CI, `deploy/smoke.py` checks the header but no browser enforces it (the e2e job runs against `vite preview`, which sends no CSP) |
| Clickjacking, MIME sniffing, referrer leaks | `frame-ancestors 'none'` and `X-Frame-Options: DENY`; `nosniff`; `Referrer-Policy: no-referrer` | `deploy/smoke.py` (checks the headers) |
| CSRF | No cookies and no sessions. A write needs a bearer token in a header, which a cross-site form cannot send. | by construction |
| Denial of service on the API | Per-address limits at the proxy (10 reads/s, burst 40; 20 connections); 16 KB bodies; client timeouts; bounded page sizes, graph limits and cursors; statement time limits for search (5 s) and analysis (10 s); fixed database pools | `test_read_timeouts.py`, analysis and search tests; validation (60 quick reads: 41 × 200, 19 × 429) |
| Information disclosure | `GET /metrics`, `/docs` and `/openapi.json` are not routed by the proxy. Problem responses never include a stack trace. The crawl policy shows the write mode, never the token or the internal origins. Logs never contain secrets or page bodies. The proxy's access log omits query strings; its error log can show one when the API is unreachable. `server_tokens off`. | `test_crawl_policy.py` (no secret, no internal origin); validation (13 paths, traversal spellings included: 404) |
| Reaching PostgreSQL or Redis | Neither publishes a port in the deployment; both require passwords, which production refuses to leave unset or at a placeholder | `docker ps` in the validation; `test_config.py` |
| A compromised application process misusing its database access | The deployed application connects as `sitescope_app`, which reads and writes rows only. It is not a superuser and not an owner, and it cannot read `alembic_version`. It cannot disable the fencing triggers (`session_replication_role`), change the schema, or run programs on the server (`COPY ... PROGRAM`). Migrations run as the owner, in a one-shot step. | `deploy/postgres/app-role.sql`; a throwaway-container test (each of those statements refused); CI job `deployment` checks the role |
| A compromised process escalating | Non-root users (10001; nginx 101); read-only root filesystems; all capabilities dropped; `no-new-privileges`; memory limits | `deploy/compose.yaml`; CI checks the image's user |
| Secrets in the repository | `.env` files git-ignored; templates hold placeholders only; CI secrets are throwaway values | Section "Secrets" below; CI job `secrets` |
| Supply chain | Exact Python versions (`requirements/*.txt`) installed with `--no-deps` and `pip check`; npm lockfile with `npm ci`; base images pinned by version; GitHub Actions pinned by commit SHA; no repository secret used by CI | `backend/Dockerfile`, `.github/workflows/` |

## Secrets

- **Where they live.** Passwords and the admin token are environment settings: `backend/.env`
  in development, `deploy/.env` in a deployment. Both are git-ignored; only the
  `.env.example` templates are committed, with empty values or `change-me` placeholders that
  production refuses.
- **No secret in the browser.** The dashboard holds no secret: `VITE_` variables are public by
  design and none is secret, and the token is never sent to or stored in the browser.
- **Scan of 2026-11-08** (before the Phase 11 commit):
  - gitleaks 8.28.0 over all 13 commits of every ref, and over the 498 files to be committed.
    One finding, reviewed as a false positive: an `Idempotency-Key` test value
    (`tests/integration/test_idempotency.py`). It is now marked `gitleaks:allow`, and its
    historical commit is listed in `.gitleaksignore`.
  - A search of every commit and of every file to be committed for the actual values of the
    development passwords: no match.
  - The only environment files ever committed are the two `.env.example` templates.
- **Continuous scanning.** The CI job `secrets` runs the same gitleaks scan over the whole
  history on every push and pull request.

## Residual risks

- **One shared admin token**, without per-person attribution or expiry. Rotation means a
  restart.
- **Per-address limits.** A distributed client gets more than the per-address limits allow.
  Behind another proxy without `real-ip.conf`, or for IPv6 visitors when Docker has no IPv6,
  every visitor shares one budget (deployment.md, "HTTPS and a domain"). There is no WAF or
  CDN in front; a public demo behind one would need its client-address handling reviewed.
- **One host.** No redundancy: a host failure stops the demo until it is restored from backup.
- **Versions, not hashes.** Python packages are pinned by version, not by hash, and base images
  by tag, not by digest. A compromised registry could serve different bytes under the same
  version. CI does not audit dependencies for known vulnerabilities. A one-off audit on
  2026-11-08 found none: `npm audit` (production and development trees) reported 0, and
  `pip-audit` 2.9.0 (in a throwaway container) found no known vulnerability in
  `requirements/runtime.txt` or `dev.txt`. New advisories appear daily, so the audit needs to
  be repeated.
- **Docker access is root access.** Anyone in the `docker` group can read the containers'
  environment, the secrets included.
- **Crawling real sites** needs egress for the crawlers. The fetcher's defenses still apply,
  but the network no longer backs them up (deployment.md, "Crawling other sites").
