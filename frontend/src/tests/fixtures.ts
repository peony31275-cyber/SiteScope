/** API objects for tests, typed by the generated schema: a fixture cannot drift from the
 * contract without failing the type check. */
import type {
  CrawlIndexing,
  CrawlJobDetail,
  CrawlJobSummary,
  CrawlPolicy,
  CrawlStatsResponse,
  GraphNodeItem,
  LinkGraphResponse,
  LinkItem,
  OverviewResponse,
  SearchResponse,
  SearchResult,
} from '../lib/api/types';

export const JOB_ID = '0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b';
export const OTHER_JOB_ID = '0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a6c';
export const PAGE_ID = '0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c';

export function summary(overrides: Partial<CrawlJobSummary> = {}): CrawlJobSummary {
  return {
    id: JOB_ID,
    status: 'running',
    seed_url: 'http://127.0.0.1:8001/',
    allowed_hosts: ['127.0.0.1'],
    max_depth: 3,
    max_pages: 100,
    current_level: 1,
    admitted_count: 6,
    failure_reason: null,
    created_at: '2026-11-07T10:00:00Z',
    updated_at: '2026-11-07T10:00:05Z',
    started_at: '2026-11-07T10:00:01Z',
    finished_at: null,
    cancel_requested_at: null,
    ...overrides,
  };
}

export function indexing(overrides: Partial<CrawlIndexing> = {}): CrawlIndexing {
  return {
    status: 'running',
    documents_total: 4,
    documents_by_status: { pending: 1, queued: 1, indexed: 2, failed: 0 },
    active: 1,
    completed_at: null,
    ...overrides,
  };
}

export function detail(overrides: Partial<CrawlJobDetail> = {}): CrawlJobDetail {
  return {
    ...summary(),
    progress: {
      urls_total: 6,
      urls_finished: 4,
      urls_by_state: {
        pending: 1,
        queued: 0,
        in_progress: 1,
        fetched: 3,
        failed: 1,
        skipped: 0,
        cancelled: 0,
      },
      pages_stored: 3,
    },
    indexing: indexing(),
    ...overrides,
  };
}

/** A finished job whose indexing is complete: nothing about it can change any more. */
export function settled(overrides: Partial<CrawlJobDetail> = {}): CrawlJobDetail {
  return detail({
    status: 'completed',
    current_level: 3,
    admitted_count: 14,
    finished_at: '2026-11-07T10:01:00Z',
    updated_at: '2026-11-07T10:01:00Z',
    progress: {
      urls_total: 14,
      urls_finished: 14,
      urls_by_state: {
        pending: 0,
        queued: 0,
        in_progress: 0,
        fetched: 13,
        failed: 0,
        skipped: 1,
        cancelled: 0,
      },
      pages_stored: 12,
    },
    indexing: indexing({
      status: 'indexed',
      documents_total: 11,
      documents_by_status: { pending: 0, queued: 0, indexed: 11, failed: 0 },
      active: 0,
      completed_at: '2026-11-07T10:01:05Z',
    }),
    ...overrides,
  });
}

export function policy(overrides: Partial<CrawlPolicy> = {}): CrawlPolicy {
  return {
    approved_hosts: ['127.0.0.1', '*.example.org'],
    allowed_schemes: ['http', 'https'],
    allowed_ports: [80, 443, 8001],
    max_depth_limit: 5,
    max_pages_limit: 1000,
    max_active_jobs: 50,
    write_access: 'open',
    ...overrides,
  };
}

export function searchResult(overrides: Partial<SearchResult> = {}): SearchResult {
  return {
    page_id: PAGE_ID,
    url: 'http://127.0.0.1:8001/docs/architecture.html',
    title: 'Architecture',
    snippet: 'Leases with fencing tokens keep workers safe',
    highlights: [{ start: 12, end: 19 }],
    score: 0.6079271,
    crawled_at: '2026-11-07T10:00:30Z',
    indexed_at: '2026-11-07T10:00:31Z',
    ...overrides,
  };
}

export function searchResponse(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    query: 'fencing',
    job_id: JOB_ID,
    searchable: true,
    total: 1,
    page: 1,
    page_size: 10,
    results: [searchResult()],
    indexing: indexing({
      status: 'indexed',
      documents_total: 11,
      documents_by_status: { pending: 0, queued: 0, indexed: 11, failed: 0 },
      active: 0,
      completed_at: '2026-11-07T10:01:05Z',
    }),
    ...overrides,
  };
}

export function stats(overrides: Partial<CrawlStatsResponse> = {}): CrawlStatsResponse {
  return {
    job_id: JOB_ID,
    status: 'completed',
    crawl: {
      urls_discovered: 15,
      urls_admitted: 14,
      urls_by_state: {
        pending: 0,
        queued: 0,
        in_progress: 0,
        fetched: 13,
        failed: 0,
        skipped: 1,
        cancelled: 0,
      },
      pages_stored: 12,
      current_depth: 3,
      max_depth: 3,
      max_pages: 100,
    },
    http_status: {
      informational_1xx: 1,
      success_2xx: 11,
      redirect_3xx: 0,
      client_error_4xx: 1,
      server_error_5xx: 0,
      no_response: 0,
    },
    depths: [
      { depth: 0, urls: 1, fetched: 1, pages: 1 },
      { depth: 1, urls: 5, fetched: 5, pages: 4 },
    ],
    indexing: indexing({
      status: 'indexed',
      documents_total: 11,
      documents_by_status: { pending: 0, queued: 0, indexed: 11, failed: 0 },
      active: 0,
      completed_at: '2026-11-07T10:01:05Z',
    }),
    links: {
      total: 26,
      internal: 24,
      external: 2,
      internal_targets: 15,
      broken: 1,
      verified: 22,
      unverified: 4,
      by_class: {
        ok: 21,
        broken: 1,
        server_error: 0,
        http_error: 0,
        redirect_error: 0,
        out_of_scope: 2,
        not_followed: 1,
        beyond_limits: 0,
        not_crawled: 0,
        robots_blocked: 1,
        unreachable: 0,
      },
    },
    most_linked: [{ url: 'http://127.0.0.1:8001/', page_id: PAGE_ID, in_links: 11 }],
    ...overrides,
  };
}

export function link(overrides: Partial<LinkItem> = {}): LinkItem {
  return {
    source_page_id: PAGE_ID,
    source_url: 'http://127.0.0.1:8001/about.html',
    target_url: 'http://127.0.0.1:8001/private/notes.html',
    anchor_text: 'Notes',
    position: 2,
    internal: true,
    nofollow: false,
    classification: 'robots_blocked',
    verified: false,
    target_crawled: false,
    target_state: 'skipped',
    target_http_status: null,
    target_page_id: null,
    final_url: null,
    final_http_status: null,
    redirect_hops: 0,
    checked_at: null,
    ...overrides,
  };
}

export function node(
  overrides: Partial<GraphNodeItem> & { id: number; url: string },
): GraphNodeItem {
  return {
    kind: 'page',
    page_id: PAGE_ID,
    title: 'Page',
    depth: 0,
    crawl_state: 'fetched',
    http_status: 200,
    in_degree: 0,
    out_degree: 1,
    ...overrides,
  };
}

export function graph(overrides: Partial<LinkGraphResponse> = {}): LinkGraphResponse {
  return {
    job_id: JOB_ID,
    nodes: [
      node({
        id: 0,
        url: 'http://127.0.0.1:8001/',
        title: 'Home',
        depth: 0,
        in_degree: 1,
        out_degree: 2,
      }),
      node({
        id: 1,
        url: 'http://127.0.0.1:8001/about.html',
        title: 'About',
        depth: 1,
        in_degree: 1,
        out_degree: 2,
      }),
      node({
        id: 2,
        url: 'http://127.0.0.1:8001/missing.html',
        kind: 'crawl_url',
        page_id: null,
        title: null,
        depth: 1,
        http_status: 404,
        in_degree: 1,
        out_degree: null,
      }),
      node({
        id: 3,
        url: 'http://127.0.0.1:8001/docs/legacy.html',
        kind: 'link_target',
        page_id: null,
        title: null,
        depth: null,
        crawl_state: null,
        http_status: null,
        in_degree: 0,
        out_degree: null,
      }),
    ],
    edges: [
      { source: 0, target: 1, nofollow: false },
      { source: 0, target: 2, nofollow: false },
      { source: 1, target: 0, nofollow: false },
    ],
    total_nodes: 4,
    total_edges: 3,
    max_nodes: 300,
    max_edges: 1500,
    truncated: false,
    ...overrides,
  };
}

export function overview(overrides: Partial<OverviewResponse> = {}): OverviewResponse {
  return {
    jobs: {
      total: 4,
      by_status: { queued: 0, running: 1, cancelling: 0, completed: 2, failed: 0, cancelled: 1 },
    },
    urls: {
      total: 52,
      by_state: {
        pending: 3,
        queued: 2,
        in_progress: 1,
        fetched: 40,
        failed: 1,
        skipped: 2,
        cancelled: 3,
      },
    },
    pages_stored: 36,
    documents: {
      total: 34,
      by_status: { pending: 1, queued: 2, indexed: 31, failed: 0 },
      active: 1,
    },
    counted_at: '2026-11-07T10:15:05Z',
    ...overrides,
  };
}

/** `list[index]`, or a test failure if it is missing (no type assertion needed). */
export function nth<T>(list: readonly T[], index: number): T {
  const item = list[index];
  if (item === undefined)
    throw new Error(`expected an item at index ${String(index)} of ${String(list.length)}`);
  return item;
}
