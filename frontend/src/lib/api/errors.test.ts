import { describe, expect, it } from 'vitest';

import { ApiError } from './client';
import { describeError, fieldName, isNotFound, isTransient } from './errors';
import type { ProblemDetails } from './types';

function problemError(status: number, code: string, extra: Partial<ProblemDetails> = {}): ApiError {
  return new ApiError({
    kind: 'problem',
    status,
    message: code,
    problem: {
      type: 'about:blank',
      title: 'Error',
      status,
      instance: '/api/v1/x',
      code,
      request_id: 'abc123',
      ...extra,
    },
  });
}

describe('isTransient', () => {
  it.each([
    [new ApiError({ kind: 'network', message: 'x' }), true],
    [new ApiError({ kind: 'timeout', message: 'x' }), false], // a slow query would only run again
    [new ApiError({ kind: 'http', status: 502, message: 'x' }), true],
    [new ApiError({ kind: 'http', status: 500, message: 'x' }), false],
    [problemError(503, 'dependency_unavailable'), true],
    [problemError(503, 'analysis_timeout'), false],
    [problemError(503, 'search_timeout'), false],
    [problemError(404, 'not_found'), false],
    [problemError(422, 'validation_failed'), false],
    [new Error('plain'), false],
  ])('%#: retries only failures that can go away on their own', (error, expected) => {
    expect(isTransient(error)).toBe(expected);
  });
});

describe('describeError', () => {
  it('names the field of each validation error, without the location prefix', () => {
    const error = problemError(422, 'validation_failed', {
      detail: 'The request failed validation.',
      errors: [
        {
          loc: ['body', 'seed_url'],
          msg: "host 'x.test' is not an approved crawl target",
          type: 'target_not_allowed',
        },
        { loc: ['query', 'page'], msg: 'page must be at most 101', type: 'page_out_of_range' },
      ],
    });
    const description = describeError(error);
    expect(description.title).toBe('The request was rejected');
    expect(description.details).toEqual([
      "seed_url: host 'x.test' is not an approved crawl target",
      'page: page must be at most 101',
    ]);
    expect(description.requestId).toBe('abc123');
  });

  it('explains timeouts and outages without the server’s internals', () => {
    expect(
      describeError(
        problemError(503, 'analysis_timeout', {
          detail: 'The analysis took longer than 10 s and was stopped.',
        }),
      ).message,
    ).toContain('took longer than 10 s');
    expect(describeError(problemError(503, 'search_timeout')).message).toContain(
      'more specific query',
    );
    const outage = describeError(problemError(503, 'dependency_unavailable'));
    expect(outage.title).toBe('A backing service is unavailable');
    expect(outage.transient).toBe(true);
    expect(describeError(new ApiError({ kind: 'network', message: 'x' })).title).toBe(
      'Cannot reach the SiteScope API',
    );
  });

  it('never shows an internal error’s details, only the request ID to quote', () => {
    const description = describeError(
      problemError(500, 'internal_error', { detail: 'secret stack' }),
    );
    expect(description.message).not.toContain('secret');
    expect(description.requestId).toBe('abc123');
  });

  it('recognizes a missing resource', () => {
    expect(isNotFound(problemError(404, 'not_found'))).toBe(true);
    expect(isNotFound(problemError(422, 'validation_failed'))).toBe(false);
  });
});

describe('fieldName', () => {
  it('drops the location kind, keeps nested parts', () => {
    expect(fieldName(['body', 'seed_url'])).toBe('seed_url');
    expect(fieldName(['header', 'Idempotency-Key'])).toBe('Idempotency-Key');
    expect(fieldName(['body'])).toBe('request');
  });
});
