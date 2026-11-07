# ADR-0006: Target allowlist and layered SSRF defenses

- Status: Accepted. Extended by [ADR-0012](0012-approved-crawl-targets.md) (the format and
  matching rules of the allowlist) and [ADR-0013](0013-safe-fetcher-pinned-connections.md) (how
  the safe fetcher implements these defenses).
- Date: 2026-11-06

## Context

A crawler sends requests to URLs that come from users (seeds) and from crawled content (links and
redirects). Without controls, it can be:

- turned against internal services and cloud metadata endpoints (SSRF);
- tricked by DNS rebinding;
- exhausted by hostile responses.

The public demo must not crawl arbitrary internet URLs.

## Decision

1. **Allowlist.** Every deployment has an explicit allowlist of crawl origins, and there is no
   "crawl anything" mode. Seeds and job scope must fall inside the allowlist. The API checks this, and
   the worker checks it again before every fetch.
2. **URL policy.**
   - `http` and `https` only;
   - no userinfo;
   - allowed ports only;
   - a maximum URL length;
   - IDNA host normalization.
3. **Resolve, validate, pin.**
   - The worker resolves DNS itself.
   - It rejects the request if *any* resolved address is in a blocked class: loopback, private,
     link-local (including the metadata address), shared/CGNAT, multicast, reserved, or unspecified.
     IPv4 addresses embedded in IPv6 forms are unwrapped and checked too.
   - It connects to the validated IP and uses the hostname only for the `Host` header, SNI, and
     certificate verification.
   - This repeats for every connection, retry, redirect target, and robots.txt fetch.
4. **Redirects.** HTTPX does not follow redirects. A redirect target re-enters the pipeline as a new URL
   and passes every check again.
5. **Bounds.**
   - per-operation timeouts plus an overall task deadline;
   - a cap on the decoded body size;
   - a content-type allowlist;
   - robots.txt (RFC 9309);
   - a per-host rate limit shared by all workers.
6. **Narrow exceptions.**
   - Private addresses are allowed only for exact origins configured as internal targets, such as the
     self-hosted demo-site container.
   - Loopback is allowed only in development and test, for local fixture servers.
   - Link-local and metadata addresses are never allowed.
7. **Defense in depth.** In the public deployment, crawler workers sit on a network that can reach only
   approved targets.

## Consequences

- Safe defaults, and a policy that table-driven tests can check, including a scripted resolver that
  simulates rebinding.
- We maintain our own resolution and connection-pinning code on top of HTTPX. IPv6 needs careful
  handling.
- By design, the public demo cannot crawl arbitrary sites.

## Alternatives considered

- **Hostname blocklists:** bypassed by DNS rebinding and alternative IP encodings.
- **Validate the address, then let the HTTP client resolve again:** leaves a time-of-check/time-of-use
  gap that rebinding exploits.
- **An egress proxy (for example, Smokescreen):** strong, but another component to run. It can be added
  later, in front of the same policy.
