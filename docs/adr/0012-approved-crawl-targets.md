# ADR-0012: Approved crawl targets are host patterns, plus allowed schemes and ports

- Status: Accepted
- Date: 2026-11-06
- Extends: [ADR-0006](0006-target-allowlist-ssrf-defenses.md)

## Context

ADR-0006 requires an explicit allowlist of crawl origins in every deployment. Phase 2C
implements the first consumer of that allowlist: `POST /api/v1/crawls` must reject seed URLs
outside it. That needs concrete answers:

- how entries are written;
- how subdomains are treated;
- how scheme and port restrictions apply;
- how development conveniences are kept out of a public deployment;
- what the API may check without contacting the network.

## Decision

1. **Configuration.**
   - `SITESCOPE_CRAWL_ALLOWED_HOSTS` lists host patterns.
   - `SITESCOPE_CRAWL_ALLOWED_SCHEMES` (default `http,https`) and
     `SITESCOPE_CRAWL_ALLOWED_PORTS` (default `80,443`) apply to every approved host.
   - A URL is approved when its host matches a pattern, its scheme is allowed, and its effective
     port (explicit, or the scheme's default) is allowed.
2. **Matching.** Entries are normalized like URL hosts: lowercase, IDNA A-labels, compressed
   IPv6. They are compared with whole host names, never as substrings.
   - `docs.example.com` approves exactly that host.
   - `*.example.com` approves every subdomain of `example.com`, at any depth, but not
     `example.com` itself.
   - A wildcard needs a base domain with at least two labels, so `*.com` is refused.
   - IP literals match exactly and take no wildcard.
   - A host with a trailing dot (`example.com.`) never matches. It is a different authority, and
     it gets a specific error message.
3. **Safe defaults.** The default host list is empty, so a fresh deployment approves nothing.
   There is no "crawl anything" entry.
4. **Development-only entries are refused in production.**
   - Development-only entries are `localhost`, names under `.localhost`, and every IP literal.
   - Settings validation rejects them when `SITESCOPE_ENVIRONMENT=production`, so the process
     does not start.
   - In every environment, entries for link-local addresses (including the 169.254.169.254
     metadata endpoint), multicast, unspecified and reserved addresses are refused.
5. **Job scope.** A job crawls exactly its seed's host: `crawl_jobs.allowed_hosts` holds that one
   host. Other subdomains, even approved ones, are outside the job's scope.
6. **Syntactic checks only.** Job creation performs no DNS lookup and no network I/O.

## Consequences

- **Defined behavior.** Operators get a short, testable policy. A public deployment can approve
  only DNS names, and only by listing them.
- **Not an SSRF defense on its own.** The check is syntactic. An approved name can resolve to a
  private address, and its DNS answers can change (DNS rebinding). The Phase 3 safe fetcher
  must resolve each host itself, reject blocked address classes, and pin the connection to the
  validated address before every request (ADR-0006). The documentation and the module say so
  explicitly.
- **Shared schemes and ports.** Schemes and ports are not configured per host. Approving both
  `https://docs.example.com` and `http://localhost:8080` in one development configuration also
  approves `http://docs.example.com:8080`. This is acceptable at this scale; per-origin entries
  remain possible later.
- **A single public interpretation.** Only the configured, normalized forms are compared, so
  numeric hosts with ambiguous parsings (`127.1`, `0x7f.0.0.1`) are rejected during URL
  normalization, before they reach the scope check.

## Alternatives considered

- **Per-origin entries (`https://docs.example.com:443`):** precise, but more verbose for the
  common case, and the subdomain wildcard would still be needed.
- **Suffix matching (`example.com` approving every subdomain):** easy to get wrong. A naive
  suffix check also accepts `evil-example.com`, and the scope of an entry is less obvious.
- **A public suffix list to forbid wildcards like `*.co.uk`:** needs a dependency and regular
  updates. The two-label rule plus operator review is enough while every entry is explicit.
- **Resolving DNS at job creation:** gives a false sense of safety (the answer can change before
  the crawl) and makes the API depend on the network.
