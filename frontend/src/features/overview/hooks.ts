/**
 * The deployment overview (`GET /api/v1/overview`, Phase 9): counts over every job, read in
 * one snapshot. Refreshed slowly (at least every 10 s: each count reads a whole table) while
 * some crawl or indexing work remains, and paused after 3 minutes' worth of reads without a
 * change (work that waits for a process that is not running; reads, not minutes, so a hidden
 * tab never counts).
 */
import { useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';

import { useSettings } from '../../app/settingsContext';
import { stallReads, useStallWatch } from '../../hooks/useStallWatch';
import { getOverview } from '../../lib/api/endpoints';
import { queryKeys } from '../../lib/api/queryKeys';
import type { OverviewResponse } from '../../lib/api/types';

const STALL_WINDOW_MS = 3 * 60_000;

export function workRemains(overview: OverviewResponse): boolean {
  const { jobs, documents } = overview;
  return (
    jobs.by_status.queued + jobs.by_status.running + jobs.by_status.cancelling > 0 ||
    documents.by_status.pending + documents.by_status.queued > 0
  );
}

function signatureOf(overview: OverviewResponse): string {
  return JSON.stringify([overview.jobs, overview.urls, overview.pages_stored, overview.documents]);
}

function intervalFor(
  overview: OverviewResponse | undefined,
  baseMs: number | null,
): number | false {
  if (overview === undefined || baseMs === null || !workRemains(overview)) return false;
  return Math.max(baseMs * 3, 10_000);
}

export function useOverview() {
  const { pollIntervalMs } = useSettings();
  const watch = useStallWatch();
  const query = useQuery({
    queryKey: queryKeys.overview,
    queryFn: ({ signal }) => getOverview(signal),
    refetchInterval: (current) =>
      watch.stalled ? false : intervalFor(current.state.data, pollIntervalMs),
  });
  const interval = intervalFor(query.data, pollIntervalMs);
  watch.observe(
    query.data === undefined ? null : signatureOf(query.data),
    query.dataUpdatedAt,
    stallReads(STALL_WINDOW_MS, interval),
  );

  const { refetch } = query;
  const { resume: resumeWatch } = watch;
  const resume = useCallback(() => {
    resumeWatch();
    void refetch();
  }, [resumeWatch, refetch]);
  return { query, polling: watch.stalled ? false : interval, stalled: watch.stalled, resume };
}
