/**
 * The dashboard's HTTP client for the SiteScope API: one place for the base URL, query
 * strings, JSON, timeouts, cancellation and RFC 9457 problem details (docs/api-conventions.md).
 *
 * The base URL is a build-time constant (`VITE_API_BASE_URL`, default `/api/v1`, same origin):
 * no address is ever taken from user input. Every request has a timeout, and a caller's
 * `AbortSignal` (TanStack Query passes one) cancels it.
 */
import type { ProblemDetails, ValidationErrorItem } from './types';

export const API_BASE_URL = normalizeBase(import.meta.env.VITE_API_BASE_URL ?? '/api/v1');
export const DEFAULT_TIMEOUT_MS = 15_000;

export type ApiErrorKind =
  /** The API answered with an RFC 9457 problem (it has `code`, `status`, `request_id`). */
  | 'problem'
  /** A non-2xx answer without a problem body, such as a proxy's 502. */
  | 'http'
  /** The request never got an answer: connection refused, DNS, offline. */
  | 'network'
  /** No answer within the request's timeout. */
  | 'timeout'
  /** A 2xx answer whose body is not the JSON the endpoint promises. */
  | 'invalid_response';

interface ApiErrorInit {
  kind: ApiErrorKind;
  message: string;
  status?: number | null;
  problem?: ProblemDetails | null;
  retryAfterSeconds?: number | null;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly problem: ProblemDetails | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: ApiErrorInit) {
    super(init.message, init.cause === undefined ? undefined : { cause: init.cause });
    this.name = 'ApiError';
    this.kind = init.kind;
    this.status = init.status ?? null;
    this.problem = init.problem ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }

  /** The problem's stable machine-readable code (`not_found`, `validation_failed`, ...). */
  get code(): string | null {
    return this.problem?.code ?? null;
  }

  /** The server's request ID, to quote when reporting a problem. */
  get requestId(): string | null {
    return this.problem?.request_id ?? null;
  }

  /** The invalid fields of a `validation_failed` problem. */
  get fieldErrors(): ValidationErrorItem[] {
    return this.problem?.errors ?? [];
  }
}

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Readonly<Record<string, QueryValue>>;

/** The URL of an API path, with a query string built by `URLSearchParams` (it encodes every
 * value); `undefined` and `null` values are left out. */
export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null) continue;
    params.append(name, String(value));
  }
  const search = params.toString();
  return `${API_BASE_URL}${path}${search ? `?${search}` : ''}`;
}

/** One path segment, encoded: an ID taken from the address bar can never add a path. */
export function segment(value: string): string {
  return encodeURIComponent(value);
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  query?: Query;
  body?: unknown;
  headers?: Readonly<Record<string, string>>;
  signal?: AbortSignal;
  timeoutMs?: number;
  /** Non-2xx statuses whose JSON body is the endpoint's own response (readiness: 503). */
  acceptStatuses?: readonly number[];
}

export interface ApiResponse<T> {
  data: T;
  status: number;
}

/**
 * Send one request and parse its JSON answer.
 *
 * Rejects with `ApiError` for every failure of the request itself; when the caller's signal
 * aborts, it rejects with the signal's reason instead (a cancellation is not an API error).
 */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResponse<T>> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timeout = new Error('timeout');
  const timer = setTimeout(() => {
    controller.abort(timeout);
  }, timeoutMs);
  const caller = options.signal;
  const forwardAbort = (): void => {
    controller.abort();
  };
  if (caller?.aborted) controller.abort();
  else caller?.addEventListener('abort', forwardAbort, { once: true });

  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';

  try {
    const response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? null : JSON.stringify(options.body),
      signal: controller.signal,
      credentials: 'same-origin',
    });
    const accepted = response.ok || (options.acceptStatuses ?? []).includes(response.status);
    if (!accepted) throw await problemOf(response);
    const text = await response.text();
    let data: unknown;
    try {
      data = text === '' ? undefined : (JSON.parse(text) as unknown);
    } catch (cause) {
      throw new ApiError({
        kind: 'invalid_response',
        status: response.status,
        message: 'The response is not valid JSON.',
        cause,
      });
    }
    return { data: data as T, status: response.status };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (caller?.aborted) throw caller.reason as Error;
    if (controller.signal.reason === timeout) {
      throw new ApiError({
        kind: 'timeout',
        message: `No response within ${String(Math.round(timeoutMs / 1000))} s.`,
        cause: error,
      });
    }
    throw new ApiError({
      kind: 'network',
      message: 'The request did not reach the API.',
      cause: error,
    });
  } finally {
    clearTimeout(timer);
    caller?.removeEventListener('abort', forwardAbort);
  }
}

async function problemOf(response: Response): Promise<ApiError> {
  const retryAfterSeconds = parseRetryAfter(response.headers.get('Retry-After'));
  const contentType = response.headers.get('Content-Type') ?? '';
  if (contentType.includes('json')) {
    try {
      const body: unknown = JSON.parse(await response.text());
      if (isProblem(body)) {
        return new ApiError({
          kind: 'problem',
          status: response.status,
          problem: body,
          retryAfterSeconds,
          message: body.detail ?? body.title,
        });
      }
    } catch {
      // Not a problem document after all: reported as a plain HTTP error below.
    }
  }
  return new ApiError({
    kind: 'http',
    status: response.status,
    retryAfterSeconds,
    message: `The server answered HTTP ${String(response.status)}.`,
  });
}

function isProblem(value: unknown): value is ProblemDetails {
  if (typeof value !== 'object' || value === null) return false;
  if (!('status' in value) || !('code' in value) || !('title' in value)) return false;
  const errors = 'errors' in value ? value.errors : undefined;
  return (
    typeof value.status === 'number' &&
    typeof value.code === 'string' &&
    typeof value.title === 'string' &&
    (errors === undefined || errors === null || Array.isArray(errors))
  );
}

/** `Retry-After` in seconds (delta-seconds form only, as the API sends it). */
export function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d{1,6}$/.test(value.trim())) return null;
  return Number(value.trim());
}

function normalizeBase(base: string): string {
  return base.endsWith('/') ? base.slice(0, -1) : base;
}
