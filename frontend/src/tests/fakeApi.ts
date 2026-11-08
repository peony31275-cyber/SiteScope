/**
 * A fake SiteScope API for component tests: a test double of `fetch` that routes requests to
 * handlers written per test, answers like the real API (JSON, RFC 9457 problems), records every
 * request, and honors abort signals. It is not the real backend: the real one is exercised by
 * the end-to-end run (e2e/, docs/development.md).
 */
import { vi } from 'vitest';

export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Headers;
  body: unknown;
}

export type Handler = (
  request: RecordedRequest,
  params: Record<string, string>,
) => Response | Promise<Response>;

interface Route {
  method: string;
  pattern: RegExp;
  names: string[];
  handler: Handler;
}

const BASE = '/api/v1';

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'test-request', ...headers },
  });
}

/** An RFC 9457 problem, as the API sends it. */
export function problem(
  status: number,
  code: string,
  extra: {
    detail?: string;
    errors?: { loc: (string | number)[]; msg: string; type: string }[];
  } = {},
  headers: Record<string, string> = {},
): Response {
  const body = {
    type: 'about:blank',
    title:
      {
        404: 'Not Found',
        409: 'Conflict',
        422: 'Unprocessable Content',
        503: 'Service Unavailable',
      }[status] ?? 'Error',
    status,
    detail: extra.detail,
    instance: '/api/v1/…',
    code,
    request_id: 'req-1234',
    ...(extra.errors === undefined ? {} : { errors: extra.errors }),
  };
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/problem+json', ...headers },
  });
}

export class FakeApi {
  readonly requests: RecordedRequest[] = [];
  readonly unhandled: RecordedRequest[] = [];
  private readonly routes: Route[] = [];

  constructor() {
    // Every page shows the API status, and job pages list recent jobs in the header.
    this.on('GET', '/health/ready', () =>
      json({
        status: 'ready',
        checks: {
          postgres: { status: 'ok', latency_ms: 1 },
          redis: { status: 'ok', latency_ms: 1 },
        },
        checked_at: '2026-11-07T10:00:00Z',
      }),
    );
    this.on('GET', '/crawls', () => json({ items: [], next_cursor: null }));
  }

  /** Add a route (the newest route for a path wins). `path` is relative to /api/v1 and may
   * name parameters: `/crawls/:jobId`. */
  on(method: string, path: string, handler: Handler): this {
    const names: string[] = [];
    const source = path.replace(/:([a-zA-Z]+)/g, (_, name: string) => {
      names.push(name);
      return '([^/]+)';
    });
    this.routes.unshift({ method, pattern: new RegExp(`^${BASE}${source}$`), names, handler });
    return this;
  }

  /** Requests to `path` (exact, or a pattern), optionally of one method. */
  calls(path: string | RegExp, method = 'GET'): RecordedRequest[] {
    return this.requests.filter(
      (request) =>
        request.method === method &&
        (typeof path === 'string' ? request.path === `${BASE}${path}` : path.test(request.path)),
    );
  }

  readonly fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(
      typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
      'http://localhost',
    );
    const method = (init.method ?? 'GET').toUpperCase();
    const body: unknown = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    const request: RecordedRequest = {
      method,
      path: url.pathname,
      query: url.searchParams,
      headers: new Headers(init.headers),
      body,
    };
    this.requests.push(request);
    const signal = init.signal ?? undefined;
    if (signal?.aborted) throw signal.reason;
    for (const route of this.routes) {
      if (route.method !== method) continue;
      const match = route.pattern.exec(url.pathname);
      if (match === null) continue;
      const params = Object.fromEntries(
        route.names.map((name, index) => [name, decodeURIComponent(match[index + 1] ?? '')]),
      );
      const response = route.handler(request, params);
      if (signal === undefined) return response;
      // A handler may hold its answer (a promise): an abort rejects the request meanwhile.
      return new Promise<Response>((resolve, reject) => {
        signal.addEventListener('abort', () => {
          reject(signal.reason as Error);
        });
        void Promise.resolve(response).then(resolve, reject);
      });
    }
    this.unhandled.push(request);
    return problem(404, 'not_found', { detail: `No fake route for ${method} ${url.pathname}` });
  };

  install(): this {
    vi.stubGlobal('fetch', this.fetch);
    return this;
  }
}
