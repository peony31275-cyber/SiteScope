# ADR-0013: The safe fetcher pins connections through httpcore2 and validates every hop

- Status: Accepted. Extended by [ADR-0015](0015-robots-txt-and-per-host-politeness.md)
  (content policies and the per-hop request gate).
- Date: 2026-11-06
- Extends: [ADR-0006](0006-target-allowlist-ssrf-defenses.md) (implements its layered defenses)

## Context

ADR-0006 requires every crawl request to be resolved, validated and pinned to the validated
address. A rebinding DNS server must not be able to change the destination between the check and
the connection. Phase 3 had to implement this with the existing stack, and settle the open choice
between `httpx` and its successor `httpx2`. The environment already has `httpx2` 2.16, on its
transport layer `httpcore2` 2.16, because Starlette's TestClient uses them.

Findings that shaped the decision:

- **No public pinning hook in `httpx2`.** Its `AsyncHTTPTransport` builds its own connection pool
  and has no parameter for a network backend. Pinning through it would mean replacing the private
  `_pool` attribute. If a later version renamed that attribute, the replacement would silently do
  nothing: requests would then resolve the host name again, and pinning would be gone without any
  error.
- **The hook in `httpcore2`.** `httpcore2.AsyncConnectionPool` accepts a `network_backend`. It
  passes the URL's host name to `connect_tcp`, then uses the same name for TLS SNI and certificate
  verification.
- **OpenSSL on Windows.** `ssl.load_default_certs()` failed to verify `https://example.com/` on
  the Windows development machine ("unable to get local issuer certificate"). Windows installs
  most root certificates only when its own verifier first needs them, so OpenSSL never sees them.

## Decision

1. **Transport.** The fetcher uses `httpcore2` directly: HTTP/1.1 only, no proxy support, no
   environment configuration, and no connection reuse between requests.
2. **Pinning.** For each request hop, `PinnedNetworkBackend` holds the validated addresses of
   exactly one origin.
   - It connects only to those addresses, in DNS order, with fallback.
   - It never resolves names.
   - It refuses any other host or port, and Unix sockets.
   - SNI, certificate verification and the `Host` header keep the original host name.
3. **Resolution and validation.**
   - The fetcher resolves each host once per hop (`getaddrinfo`), and refuses the host if *any*
     address is not allowed (fail closed).
   - Addresses are classified with the IPv4 forms embedded in IPv6 unwrapped (mapped, NAT64).
   - Global addresses are allowed.
   - Private addresses are allowed only for configured internal origins
     (`SITESCOPE_FETCH_INTERNAL_ORIGINS`); loopback likewise, and never in production.
   - Metadata, link-local, shared, multicast, unspecified and reserved addresses are never
     allowed.
4. **Redirects.**
   - The HTTP stack never follows redirects. The fetcher follows up to
     `SITESCOPE_FETCH_MAX_REDIRECTS` of them.
   - Each target goes through normalization, the crawl scope, resolution, validation and pinning
     again.
   - A loop or an exceeded limit is a failure.
   - `max_redirects=0` returns the redirect instead of following it, for crawlers that admit
     redirect targets themselves (architecture section 8.5).
5. **TLS.** `truststore.SSLContext` (TLS 1.2 or newer): the operating system's verifier checks
   the certificate chain and the host name. `truststore` is already a dependency of `httpcore2`.
6. **Bounds.**
   - Timeouts:
     - a connect timeout, which also bounds DNS and the TLS handshake;
     - a read timeout;
     - an overall deadline for the whole fetch.
   - A wire limit and a decoded-size limit. A body over a limit is rejected, never truncated.
   - Decompression in 64 KiB slices, for `gzip` and `deflate` only (the codings the fetcher
     advertises).
   - HTML media types only.
7. **Failures.** One `FetchError` with a `FailureKind` (snake_case, so it fits
   `crawl_urls.outcome`) and a `retryable` flag. Retryable failures are:
   - temporary DNS failures;
   - connection errors and timeouts;
   - truncated bodies;
   - HTTP 429, 500, 502, 503 and 504.

## Consequences

- **Rebinding needs a second resolution, and there is none.** Tests show it with a host name the
  operating system cannot resolve, a scripted resolver whose second answer is the metadata
  address, and a recording backend that captures every address actually connected to. They also
  check the SNI and `Host` header that a real TLS server received.
- **No `httpx2` client features.** Cookies, authentication, proxies and redirect handling are
  absent. A crawler needs none of them, and each would be a way around the policy.
- **Coupling to `httpcore2` internals.** The fetcher depends on two of them:
  - the message text of h11's truncation error, which separates a retryable `truncated` failure
    from a permanent `malformed_response`;
  - the pool re-raising connect errors `from None`, so TLS causes are found through
    `__context__`.

  Tests cover both, so an upgrade that changes them fails loudly.
- **Platform-dependent TLS.** Verification follows the platform, as in a browser. On Linux,
  `truststore` uses OpenSSL with the system store, which honors `SSL_CERT_FILE` and
  `SSL_CERT_DIR`.
- **Limits that remain.**
  - Robots.txt and the per-host rate limit are not part of the fetcher yet (Phase 4).
  - Network isolation of the workers, the last line of defense, belongs to the deployment
    (Phase 11).
  - An internal origin trusts the DNS of its own host name.

## Alternatives considered

- **The `httpx2` client with a replaced private `_pool`:** rejected, because a rename in a later
  version would silently disable pinning.
- **Validate, then let the client resolve again:** the time-of-check/time-of-use gap that DNS
  rebinding exploits (already rejected in ADR-0006).
- **Rewrite the URL to the IP address, with a `Host` header and the `sni_hostname` extension:**
  workable, but every URL, log line, redirect target and error would carry the address instead of
  the name, and each request path would have to rebuild the name for TLS correctly.
- **The `certifi` CA bundle instead of `truststore`:** a new dependency with its own update
  cycle, and it ignores the operating system's trust settings.
- **A streaming extractor on the stdlib tokenizer instead of BeautifulSoup:** faster and smaller
  in memory, but more custom code to maintain. The architecture already chose BeautifulSoup with
  `html.parser`, and input limits bound its cost.
- **An egress proxy (for example, Smokescreen):** strong, but another component to run. It can
  still be added in front of the same policy.
