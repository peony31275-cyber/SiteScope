/**
 * One typed function per API operation (operation IDs from the OpenAPI document). Request and
 * response types are the generated ones; path IDs are encoded as single segments.
 */
import { request, segment } from './client';
import type {
  BrokenLinkList,
  BrokenLinksQuery,
  CrawlJobDetail,
  CrawlJobList,
  CrawlJobSummary,
  CrawlPolicy,
  CrawlStatsResponse,
  CreateCrawlJobRequest,
  LinkGraphQuery,
  LinkGraphResponse,
  LinkList,
  LinksQuery,
  ListCrawlsQuery,
  OverviewResponse,
  PageList,
  PagesQuery,
  ReadinessResponse,
  SearchQuery,
  SearchResponse,
} from './types';

/** Search, analysis and the overview have server-side time limits of at most 60 s
 * (`SITESCOPE_SEARCH_TIMEOUT_SECONDS`, `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`): the client waits
 * a little longer, so the server's own answer (`503 ..._timeout`) arrives first. */
const QUERY_TIMEOUT_MS = 65_000;

/** `GET /health/ready`: 200 when PostgreSQL and Redis answer, 503 (same body) otherwise. */
export async function getReadiness(signal?: AbortSignal): Promise<ReadinessResponse> {
  const response = await request<ReadinessResponse>('/health/ready', {
    signal,
    timeoutMs: 8_000,
    acceptStatuses: [503],
  });
  return response.data;
}

export async function getOverview(signal?: AbortSignal): Promise<OverviewResponse> {
  return (await request<OverviewResponse>('/overview', { signal, timeoutMs: QUERY_TIMEOUT_MS }))
    .data;
}

export async function getCrawlPolicy(signal?: AbortSignal): Promise<CrawlPolicy> {
  return (await request<CrawlPolicy>('/crawl-policy', { signal })).data;
}

export async function listCrawls(
  query: ListCrawlsQuery,
  signal?: AbortSignal,
): Promise<CrawlJobList> {
  return (await request<CrawlJobList>('/crawls', { query, signal })).data;
}

export async function getCrawl(jobId: string, signal?: AbortSignal): Promise<CrawlJobDetail> {
  return (await request<CrawlJobDetail>(`/crawls/${segment(jobId)}`, { signal })).data;
}

export interface CreatedCrawl {
  job: CrawlJobSummary;
  /** An earlier request with the same idempotency key created this job (200, not 201). */
  replayed: boolean;
}

/** `POST /crawls`. The idempotency key makes a retry of the same submission safe: the server
 * returns the job the first attempt created instead of creating another. */
export async function createCrawl(
  body: CreateCrawlJobRequest,
  idempotencyKey: string,
): Promise<CreatedCrawl> {
  const response = await request<CrawlJobSummary>('/crawls', {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
  return { job: response.data, replayed: response.status === 200 };
}

export interface CancelledCrawl {
  job: CrawlJobSummary;
  /** 202: the job is `cancelling`, its in-flight URLs are still finishing. */
  settling: boolean;
}

export async function cancelCrawl(jobId: string): Promise<CancelledCrawl> {
  const response = await request<CrawlJobSummary>(`/crawls/${segment(jobId)}/cancel`, {
    method: 'POST',
  });
  return { job: response.data, settling: response.status === 202 };
}

export async function searchPages(
  query: SearchQuery,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  return (await request<SearchResponse>('/search', { query, signal, timeoutMs: QUERY_TIMEOUT_MS }))
    .data;
}

export async function getCrawlStats(
  jobId: string,
  signal?: AbortSignal,
): Promise<CrawlStatsResponse> {
  const path = `/crawls/${segment(jobId)}/stats`;
  return (await request<CrawlStatsResponse>(path, { signal, timeoutMs: QUERY_TIMEOUT_MS })).data;
}

export async function listCrawlPages(
  jobId: string,
  query: PagesQuery,
  signal?: AbortSignal,
): Promise<PageList> {
  const path = `/crawls/${segment(jobId)}/pages`;
  return (await request<PageList>(path, { query, signal, timeoutMs: QUERY_TIMEOUT_MS })).data;
}

export async function listCrawlLinks(
  jobId: string,
  query: LinksQuery,
  signal?: AbortSignal,
): Promise<LinkList> {
  const path = `/crawls/${segment(jobId)}/links`;
  return (await request<LinkList>(path, { query, signal, timeoutMs: QUERY_TIMEOUT_MS })).data;
}

export async function listCrawlBrokenLinks(
  jobId: string,
  query: BrokenLinksQuery,
  signal?: AbortSignal,
): Promise<BrokenLinkList> {
  const path = `/crawls/${segment(jobId)}/broken-links`;
  return (await request<BrokenLinkList>(path, { query, signal, timeoutMs: QUERY_TIMEOUT_MS })).data;
}

export async function getCrawlLinkGraph(
  jobId: string,
  query: LinkGraphQuery,
  signal?: AbortSignal,
): Promise<LinkGraphResponse> {
  const path = `/crawls/${segment(jobId)}/link-graph`;
  return (await request<LinkGraphResponse>(path, { query, signal, timeoutMs: QUERY_TIMEOUT_MS }))
    .data;
}
