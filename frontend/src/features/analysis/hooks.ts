/**
 * Website-analysis queries (Phase 8 endpoints). The listings page with opaque cursors, and
 * keep the previous page on screen while the next loads, within one job only. Statistics are
 * refreshed slowly (at least every 10 s: they count every link of the job) while the crawl or
 * its indexing runs, never once both are over, and they pause after 3 minutes' worth of reads
 * without change (as a job does, `useStallWatch`).
 */
import { useQuery } from '@tanstack/react-query';

import { useSettings } from '../../app/settingsContext';
import { stallReads, useStallWatch } from '../../hooks/useStallWatch';
import {
  getCrawlLinkGraph,
  getCrawlStats,
  listCrawlBrokenLinks,
  listCrawlLinks,
  listCrawlPages,
} from '../../lib/api/endpoints';
import { queryKeys } from '../../lib/api/queryKeys';
import type {
  BrokenLinksQuery,
  CrawlStatsResponse,
  LinkGraphQuery,
  LinksQuery,
  PagesQuery,
} from '../../lib/api/types';
import { isCrawlActive } from '../crawls/status';

/** Keep showing the previous result while the next one loads, unless it belongs to another
 * job (it would show the wrong job's data). */
function sameJobPlaceholder(jobId: string) {
  return <T>(
    previous: T | undefined,
    previousQuery: { queryKey: readonly unknown[] } | undefined,
  ): T | undefined => (previousQuery?.queryKey[1] === jobId ? previous : undefined);
}

function statsInterval(
  stats: CrawlStatsResponse | undefined,
  baseMs: number | null,
): number | false {
  if (stats === undefined || baseMs === null) return false;
  const running = isCrawlActive(stats.status) || stats.indexing.completed_at === null;
  return running ? Math.max(baseMs * 3, 10_000) : false;
}

export function useCrawlStats(jobId: string) {
  const { pollIntervalMs } = useSettings();
  const watch = useStallWatch();
  const query = useQuery({
    queryKey: queryKeys.stats(jobId),
    queryFn: ({ signal }) => getCrawlStats(jobId, signal),
    refetchInterval: (current) =>
      watch.stalled ? false : statsInterval(current.state.data, pollIntervalMs),
  });
  const stats = query.data;
  watch.observe(
    stats === undefined ? null : JSON.stringify(stats),
    query.dataUpdatedAt,
    stallReads(3 * 60_000, statsInterval(stats, pollIntervalMs)),
  );
  return query;
}

export function useCrawlPages(jobId: string, query: PagesQuery) {
  return useQuery({
    queryKey: queryKeys.pages(jobId, query),
    queryFn: ({ signal }) => listCrawlPages(jobId, query, signal),
    placeholderData: sameJobPlaceholder(jobId),
  });
}

export function useCrawlLinks(jobId: string, query: LinksQuery) {
  return useQuery({
    queryKey: queryKeys.links(jobId, query),
    queryFn: ({ signal }) => listCrawlLinks(jobId, query, signal),
    placeholderData: sameJobPlaceholder(jobId),
  });
}

export function useCrawlBrokenLinks(jobId: string, query: BrokenLinksQuery) {
  return useQuery({
    queryKey: queryKeys.brokenLinks(jobId, query),
    queryFn: ({ signal }) => listCrawlBrokenLinks(jobId, query, signal),
    placeholderData: sameJobPlaceholder(jobId),
  });
}

/** The link graph. Not polled (a layout should not jump); refreshed on request. */
export function useLinkGraph(jobId: string, query: LinkGraphQuery) {
  return useQuery({
    queryKey: queryKeys.linkGraph(jobId, query),
    queryFn: ({ signal }) => getCrawlLinkGraph(jobId, query, signal),
    staleTime: 60_000,
  });
}
