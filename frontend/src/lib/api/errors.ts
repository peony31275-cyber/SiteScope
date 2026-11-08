/**
 * What an API failure means for the person using the dashboard: a title, a message, and the
 * request ID to quote. Messages never include a stack trace or raw server output.
 */
import { ApiError } from './client';
import type { ValidationErrorItem } from './types';

export interface ErrorDescription {
  title: string;
  message: string;
  /** One line per invalid field of a `validation_failed` problem. */
  details: string[];
  requestId: string | null;
  /** The failure is likely to go away on its own: retrying makes sense. */
  transient: boolean;
}

const GATEWAY_STATUSES = new Set([502, 503, 504]);

/** Whether retrying the same request may succeed at once: the API or a backing service was
 * briefly unreachable. Validation errors, missing jobs, query time limits and client timeouts
 * (a slow query would only run again) are not retried automatically. */
export function isTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  switch (error.kind) {
    case 'network':
      return true;
    case 'timeout':
      return false;
    case 'http':
      return error.status !== null && GATEWAY_STATUSES.has(error.status);
    case 'problem':
      return error.code === 'dependency_unavailable';
    case 'invalid_response':
      return false;
  }
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.kind === 'problem' && error.status === 404;
}

export function fieldName(loc: ValidationErrorItem['loc']): string {
  const parts = loc.filter(
    (part, index) =>
      !(
        index === 0 &&
        typeof part === 'string' &&
        ['body', 'query', 'path', 'header'].includes(part)
      ),
  );
  return parts.length === 0 ? 'request' : parts.map(String).join('.');
}

/** `read`: a query, which the dashboard retries or refreshes by itself. `action`: a request
 * that changes something (create, cancel); it is never repeated automatically, and after a
 * failure its effect may be unknown. */
export type ErrorContext = 'read' | 'action';

export function describeError(error: unknown, context: ErrorContext = 'read'): ErrorDescription {
  if (!(error instanceof ApiError)) {
    return {
      title: 'Something went wrong',
      message: error instanceof Error ? error.message : 'An unexpected error occurred.',
      details: [],
      requestId: null,
      transient: false,
    };
  }
  const base = {
    details: [] as string[],
    requestId: error.requestId,
    transient: isTransient(error),
  };
  switch (error.kind) {
    case 'network':
      return {
        ...base,
        title: 'Cannot reach the SiteScope API',
        message:
          'The request got no answer. Check that the API is running and that this computer is ' +
          (context === 'read'
            ? 'online; the dashboard retries by itself.'
            : 'online, then try again.'),
      };
    case 'timeout':
      return {
        ...base,
        title: 'The API did not answer in time',
        message: `${error.message} The server may be overloaded or restarting${
          context === 'action' ? '; the request may still have taken effect.' : '.'
        }`,
      };
    case 'invalid_response':
      return {
        ...base,
        title: 'Unreadable response',
        message: 'The API answered, but not with the JSON this page expects.',
      };
    case 'http':
      return {
        ...base,
        title:
          error.status !== null && GATEWAY_STATUSES.has(error.status)
            ? 'The API is unreachable'
            : 'Unexpected response',
        message:
          error.status !== null && GATEWAY_STATUSES.has(error.status)
            ? `The proxy in front of the API answered HTTP ${String(error.status)}: the API ` +
              'server is probably stopped or restarting.'
            : `The server answered HTTP ${String(error.status)} without error details.`,
      };
    case 'problem':
      return { ...base, ...describeProblem(error, context) };
  }
}

function describeProblem(
  error: ApiError,
  context: ErrorContext,
): Pick<ErrorDescription, 'title' | 'message' | 'details'> {
  const detail = error.problem?.detail ?? null;
  switch (error.code) {
    case 'dependency_unavailable':
      return {
        title: 'A backing service is unavailable',
        message:
          'PostgreSQL or Redis is temporarily unavailable, so the API could not answer. ' +
          (context === 'read'
            ? 'The dashboard retries by itself.'
            : 'Check the current state, then try again.'),
        details: [],
      };
    case 'analysis_timeout':
      return {
        title: 'The analysis took too long',
        message: `${detail ?? 'The analysis was stopped.'} Large crawl jobs can exceed the server's analysis time limit.`,
        details: [],
      };
    case 'search_timeout':
      return {
        title: 'The search took too long',
        message: `${detail ?? 'The search was stopped.'} Try a more specific query.`,
        details: [],
      };
    case 'not_found':
      return { title: 'Not found', message: detail ?? 'The resource does not exist.', details: [] };
    case 'validation_failed':
      return {
        title: 'The request was rejected',
        message: detail ?? 'The request failed validation.',
        details: error.fieldErrors.map((item) => `${fieldName(item.loc)}: ${item.msg}`),
      };
    case 'idempotency_key_reused':
      return {
        title: 'Conflicting request',
        message: detail ?? 'This request conflicts with an earlier one.',
        details: [],
      };
    case 'internal_error':
      return {
        title: 'Unexpected server error',
        message:
          'The API failed while handling the request. The details are in the server log; ' +
          'quote the request ID when reporting it.',
        details: [],
      };
    default:
      return {
        title: error.problem?.title ?? 'Request failed',
        message: detail ?? error.message,
        details: [],
      };
  }
}
