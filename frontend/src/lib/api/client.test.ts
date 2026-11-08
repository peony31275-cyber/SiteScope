import { describe, expect, it, vi } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { ApiError, buildUrl, parseRetryAfter, request, segment } from './client';
import { getReadiness } from './endpoints';

describe('buildUrl', () => {
  it('puts the path under the API base and leaves out empty parameters', () => {
    expect(buildUrl('/crawls', { limit: 20, cursor: null, depth: undefined })).toBe(
      '/api/v1/crawls?limit=20',
    );
    expect(buildUrl('/overview')).toBe('/api/v1/overview');
  });

  it('encodes every query value, so input cannot add parameters', () => {
    const url = new URL(
      buildUrl('/search', { q: 'a&b=c "quoted" #frag', internal: false }),
      'http://x',
    );
    expect(url.searchParams.get('q')).toBe('a&b=c "quoted" #frag');
    expect(url.searchParams.get('internal')).toBe('false');
    expect([...url.searchParams.keys()]).toEqual(['q', 'internal']);
  });

  it('encodes a path ID as one segment', () => {
    expect(`/crawls/${segment('../health/ready?x=1')}`).toBe('/crawls/..%2Fhealth%2Fready%3Fx%3D1');
  });
});

describe('request', () => {
  it('parses the JSON of a successful answer', async () => {
    const api = new FakeApi().on('GET', '/overview', () => json({ pages_stored: 3 })).install();
    await expect(request('/overview')).resolves.toEqual({ data: { pages_stored: 3 }, status: 200 });
    expect(api.calls('/overview')[0]?.headers.get('Accept')).toBe('application/json');
  });

  it('turns a problem document into an ApiError with its code, request ID and field errors', async () => {
    new FakeApi()
      .on('POST', '/crawls', () =>
        problem(422, 'validation_failed', {
          detail: 'The request failed validation.',
          errors: [
            {
              loc: ['body', 'max_depth'],
              msg: 'max_depth must be at most 5 on this server',
              type: 'limit_exceeded',
            },
          ],
        }),
      )
      .install();

    const error = await request('/crawls', { method: 'POST', body: {} }).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect([apiError.kind, apiError.status, apiError.code, apiError.requestId]).toEqual([
      'problem',
      422,
      'validation_failed',
      'req-1234',
    ]);
    expect(apiError.fieldErrors[0]?.type).toBe('limit_exceeded');
  });

  it('keeps Retry-After of an outage', async () => {
    new FakeApi()
      .on('GET', '/overview', () =>
        problem(503, 'dependency_unavailable', {}, { 'Retry-After': '5' }),
      )
      .install();
    const error = (await request('/overview').catch((caught: unknown) => caught)) as ApiError;
    expect([error.code, error.retryAfterSeconds]).toEqual(['dependency_unavailable', 5]);
  });

  it('reports a non-problem error (a proxy page) as an HTTP error', async () => {
    new FakeApi()
      .on(
        'GET',
        '/overview',
        () =>
          new Response('<html>Bad Gateway</html>', {
            status: 502,
            headers: { 'Content-Type': 'text/html' },
          }),
      )
      .install();
    const error = (await request('/overview').catch((caught: unknown) => caught)) as ApiError;
    expect([error.kind, error.status, error.problem]).toEqual(['http', 502, null]);
  });

  it('reports an unreachable server as a network error', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
    const error = (await request('/overview').catch((caught: unknown) => caught)) as ApiError;
    expect(error.kind).toBe('network');
  });

  it('reports a 200 whose body is not JSON as an invalid response', async () => {
    new FakeApi().on('GET', '/overview', () => new Response('not json', { status: 200 })).install();
    const error = (await request('/overview').catch((caught: unknown) => caught)) as ApiError;
    expect(error.kind).toBe('invalid_response');
  });

  it('gives up after its timeout', async () => {
    new FakeApi().on('GET', '/overview', () => new Promise<Response>(() => undefined)).install();
    const error = (await request('/overview', { timeoutMs: 20 }).catch(
      (caught: unknown) => caught,
    )) as ApiError;
    expect(error.kind).toBe('timeout');
  });

  it("rejects with the caller's abort reason, not an API error, when the caller cancels", async () => {
    new FakeApi().on('GET', '/overview', () => new Promise<Response>(() => undefined)).install();
    const controller = new AbortController();
    const pending = request('/overview', { signal: controller.signal });
    const reason = new DOMException('The page moved on', 'AbortError');
    controller.abort(reason);
    await expect(pending).rejects.toBe(reason);
  });

  it('accepts the readiness probe’s 503, whose body is its own schema', async () => {
    new FakeApi()
      .on('GET', '/health/ready', () =>
        json(
          {
            status: 'not_ready',
            checks: {
              postgres: { status: 'ok', latency_ms: 2 },
              redis: { status: 'unavailable', latency_ms: 0.4 },
            },
            checked_at: '2026-11-07T10:00:00Z',
          },
          503,
        ),
      )
      .install();
    await expect(getReadiness()).resolves.toMatchObject({ status: 'not_ready' });
  });
});

describe('parseRetryAfter', () => {
  it('reads delta-seconds only', () => {
    expect(parseRetryAfter('5')).toBe(5);
    expect(parseRetryAfter(' 30 ')).toBe(30);
    expect(parseRetryAfter('Wed, 21 Oct 2015 07:28:00 GMT')).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });
});
