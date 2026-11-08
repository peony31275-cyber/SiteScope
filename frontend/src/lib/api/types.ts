/**
 * Names for the API's schemas. Every type comes from `schema.d.ts`, which openapi-typescript
 * generates from the backend's OpenAPI document (`openapi.json`; `npm run api:types`). Nothing
 * here is written by hand, so the dashboard cannot drift from the contract unnoticed: the
 * backend test `tests/unit/api/test_openapi_snapshot.py` fails when the snapshot is stale.
 */
import type { components, operations } from './schema';

type Schemas = components['schemas'];

export type JobStatus = Schemas['JobStatus'];
export type UrlState = Schemas['UrlState'];
export type IndexingState = Schemas['IndexingState'];
export type LinkClass = Schemas['LinkClass'];
export type PageIndexStatus = Schemas['PageIndexStatus'];
export type NodeKind = Schemas['NodeKind'];

export type CrawlJobSummary = Schemas['CrawlJobSummary'];
export type CrawlJobDetail = Schemas['CrawlJobDetail'];
export type CrawlJobList = Schemas['CrawlJobList'];
export type CrawlIndexing = Schemas['CrawlIndexing'];
export type UrlStateCounts = Schemas['UrlStateCounts'];
export type CreateCrawlJobRequest = Schemas['CreateCrawlJobRequest'];
export type CrawlPolicy = Schemas['CrawlPolicy'];
export type OverviewResponse = Schemas['OverviewResponse'];
export type ReadinessResponse = Schemas['ReadinessResponse'];

export type SearchResponse = Schemas['SearchResponse'];
export type SearchResult = Schemas['SearchResult'];
export type HighlightRange = Schemas['HighlightRange'];

export type CrawlStatsResponse = Schemas['CrawlStatsResponse'];
export type PageList = Schemas['PageList'];
export type PageItem = Schemas['PageItem'];
export type LinkList = Schemas['LinkList'];
export type LinkItem = Schemas['LinkItem'];
export type BrokenLinkList = Schemas['BrokenLinkList'];
export type BrokenLinkItem = Schemas['BrokenLinkItem'];
export type LinkGraphResponse = Schemas['LinkGraphResponse'];
export type GraphNodeItem = Schemas['GraphNodeItem'];
export type GraphEdgeItem = Schemas['GraphEdgeItem'];

export type ProblemDetails = Schemas['ProblemDetails'];
export type ValidationErrorItem = Schemas['ValidationErrorItem'];

type QueryOf<Operation extends keyof operations> = NonNullable<
  operations[Operation]['parameters']['query']
>;

export type ListCrawlsQuery = QueryOf<'list_crawls'>;
export type SearchQuery = QueryOf<'search_pages'>;
export type PagesQuery = QueryOf<'list_crawl_pages'>;
export type LinksQuery = QueryOf<'list_crawl_links'>;
export type BrokenLinksQuery = QueryOf<'list_crawl_broken_links'>;
export type LinkGraphQuery = QueryOf<'get_crawl_link_graph'>;
