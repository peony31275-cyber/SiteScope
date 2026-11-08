/** TanStack Query keys: one place, so invalidation and the per-key request deduplication agree. */
import type {
  BrokenLinksQuery,
  LinkGraphQuery,
  LinksQuery,
  PagesQuery,
  SearchQuery,
} from './types';

export const queryKeys = {
  readiness: ['readiness'] as const,
  overview: ['overview'] as const,
  crawlPolicy: ['crawl-policy'] as const,
  crawls: ['crawls'] as const,
  crawlList: (limit: number, cursor: string | null) =>
    ['crawls', 'list', { limit, cursor }] as const,
  crawl: (jobId: string) => ['crawls', 'detail', jobId] as const,
  analysis: (jobId: string) => ['analysis', jobId] as const,
  stats: (jobId: string) => ['analysis', jobId, 'stats'] as const,
  pages: (jobId: string, query: PagesQuery) => ['analysis', jobId, 'pages', query] as const,
  links: (jobId: string, query: LinksQuery) => ['analysis', jobId, 'links', query] as const,
  brokenLinks: (jobId: string, query: BrokenLinksQuery) =>
    ['analysis', jobId, 'broken-links', query] as const,
  linkGraph: (jobId: string, query: LinkGraphQuery) => ['analysis', jobId, 'graph', query] as const,
  search: (query: SearchQuery) => ['search', query] as const,
};
