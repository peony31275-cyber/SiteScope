/**
 * The crawl policy, applied in the browser to guide the form (ADR-0012): approved host
 * patterns, allowed schemes and ports, and the job limits. The server checks every request
 * again and is the only authority; these checks only spare a round trip and explain a refusal
 * before it happens. Values are never changed to fit: an invalid value is reported.
 */
import type { CrawlPolicy } from '../../lib/api/types';

/** As in the API (`SEED_URL_MAX_LENGTH`): the URL as submitted, before normalization. */
export const SEED_URL_MAX_LENGTH = 4096;

const DEFAULT_PORTS: Record<string, number> = { 'http:': 80, 'https:': 443 };

function hostMatches(host: string, pattern: string): boolean {
  if (pattern.startsWith('*.')) {
    const base = pattern.slice(2);
    return host.endsWith(`.${base}`) && host.length > base.length + 1;
  }
  return host === pattern;
}

/** Why the seed URL is not acceptable under the policy, or null if it is. */
export function seedUrlProblem(raw: string, policy: CrawlPolicy): string | null {
  const value = raw.trim();
  if (value === '') return 'Enter the URL the crawl starts from.';
  if (raw.length > SEED_URL_MAX_LENGTH)
    return `The URL is longer than ${String(SEED_URL_MAX_LENGTH)} characters.`;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return 'Enter an absolute URL, such as https://docs.example.com/.';
  }
  const scheme = url.protocol.slice(0, -1);
  if (!(policy.allowed_schemes as readonly string[]).includes(scheme)) {
    return `The scheme "${scheme}" is not allowed on this server (allowed: ${policy.allowed_schemes.join(', ')}).`;
  }
  if (url.username !== '' || url.password !== '')
    return 'URLs with a user name or password are not accepted.';
  // As the server normalizes hosts: lowercase, IDNA A-labels, IPv6 in brackets ([::1]).
  const host = url.hostname.toLowerCase();
  if (!policy.approved_hosts.some((pattern) => hostMatches(host, pattern))) {
    return `"${host}" is not an approved crawl target on this server.`;
  }
  const port = url.port === '' ? DEFAULT_PORTS[url.protocol] : Number(url.port);
  if (port === undefined || !policy.allowed_ports.includes(port)) {
    return `Port ${String(port)} is not allowed on this server (allowed: ${policy.allowed_ports.join(', ')}).`;
  }
  return null;
}

/** Why a limit is not acceptable, or null. `raw` is the input's text: only whole numbers in
 * range pass; nothing is rounded or clamped. */
export function limitProblem(
  raw: string,
  { min, max, name }: { min: number; max: number; name: string },
): string | null {
  const value = raw.trim();
  if (value === '') return `Enter the ${name}.`;
  if (!/^\d+$/.test(value)) return `The ${name} must be a whole number.`;
  const number = Number(value);
  if (number < min || number > max) {
    return `The ${name} must be between ${String(min)} and ${String(max)} on this server.`;
  }
  return null;
}

/** Seed URLs to offer: the root of every exact approved host, with each allowed scheme and
 * port (default ports omitted; http on 443 and https on 80 skipped). Wildcard patterns need a
 * concrete subdomain, so they are listed in the help text instead. */
export function suggestedSeeds(policy: CrawlPolicy, limit = 12): string[] {
  const seeds: string[] = [];
  for (const host of policy.approved_hosts) {
    if (host.startsWith('*.')) continue;
    for (const scheme of policy.allowed_schemes) {
      for (const port of policy.allowed_ports) {
        if ((scheme === 'http' && port === 443) || (scheme === 'https' && port === 80)) continue;
        const defaultPort = DEFAULT_PORTS[`${scheme}:`] === port;
        seeds.push(`${scheme}://${host}${defaultPort ? '' : `:${String(port)}`}/`);
      }
    }
  }
  return seeds.slice(0, limit);
}

/** An idempotency key for one submission (the same key for its retries). */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // Outside a secure context randomUUID is missing; getRandomValues is not.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}
