/**
 * The eleven link classes of the API (ADR-0019, docs/api-conventions.md, "Link
 * classification"). Only an HTTP response verifies a class. Unverified links (never requested,
 * or no response) are shown in neutral gray: they are not working, and not broken either.
 * Only `broken` (404 or 410 at the end of the redirects) is a confirmed broken link.
 */
import type { Tone } from '../../components/tones';
import type { LinkClass } from '../../lib/api/types';

export interface LinkClassMeaning {
  label: string;
  verified: boolean;
  tone: Tone;
  description: string;
}

export const LINK_CLASSES: Record<LinkClass, LinkClassMeaning> = {
  ok: { label: 'OK', verified: true, tone: 'good', description: 'The final response was 2xx.' },
  broken: {
    label: 'Broken',
    verified: true,
    tone: 'critical',
    description: 'Confirmed broken: the final response was 404 Not Found or 410 Gone.',
  },
  server_error: {
    label: 'Server error',
    verified: true,
    tone: 'serious',
    description:
      'The final response was 5xx, 408 or 429 after the crawl’s retries: possibly transient, not proven missing.',
  },
  http_error: {
    label: 'HTTP error',
    verified: true,
    tone: 'warning',
    description:
      'Another final status, such as 401 or 403: the server refused, which does not prove the page is missing.',
  },
  redirect_error: {
    label: 'Redirect error',
    verified: true,
    tone: 'warning',
    description: 'A redirect loop, more than 20 redirects, or a 3xx without a usable Location.',
  },
  out_of_scope: {
    label: 'Out of scope',
    verified: false,
    tone: 'neutral',
    description:
      'External, not an approved target, or redirected off the job’s hosts: never requested.',
  },
  not_followed: {
    label: 'Not followed',
    verified: false,
    tone: 'neutral',
    description: 'rel="nofollow", or on a nofollow page: never admitted, never requested.',
  },
  beyond_limits: {
    label: 'Beyond limits',
    verified: false,
    tone: 'neutral',
    description: 'One level past max_depth, or the page budget was used up: never requested.',
  },
  not_crawled: {
    label: 'Not crawled',
    verified: false,
    tone: 'neutral',
    description:
      'Not requested (yet): pending, queued, in progress or cancelled, or for a reason the results do not record.',
  },
  robots_blocked: {
    label: 'Robots blocked',
    verified: false,
    tone: 'neutral',
    description:
      'robots.txt disallowed the target, or robots.txt could not be fetched: never requested.',
  },
  unreachable: {
    label: 'Unreachable',
    verified: false,
    tone: 'neutral',
    description:
      'The request ended without a response: DNS, connection, TLS or protocol error, or a timeout.',
  },
};

/** Verified classes first (broken first among them), then the unverified ones. */
export const LINK_CLASS_ORDER: readonly LinkClass[] = [
  'broken',
  'server_error',
  'http_error',
  'redirect_error',
  'ok',
  'not_crawled',
  'beyond_limits',
  'not_followed',
  'robots_blocked',
  'out_of_scope',
  'unreachable',
];
