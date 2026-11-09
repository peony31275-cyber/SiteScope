/**
 * Server state of crawl jobs (TanStack Query).
 *
 * Polling: a job is refreshed while it can still change (`jobRefetchInterval`), never while
 * its tab is hidden, and never after it settles. TanStack Query runs one request per key at a
 * time and cancels an older one when it starts a newer one (an invalidation), so a stale
 * response cannot overwrite a fresh one; unmounting the last observer stops the polling.
 *
 * Stall guard: a job that keeps showing no change over `stallWindowMs` worth of reads pauses
 * its polling (`stalled`): it is probably waiting for a process that is not running. Reads are
 * counted, not minutes, so time in a hidden tab never counts. New data or `resume()` restarts.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useCallback } from 'react';

import { useSettings } from '../../app/settingsContext';
import { stallReads, useStallWatch } from '../../hooks/useStallWatch';
import {
  cancelCrawl,
  createCrawl,
  getCrawl,
  getCrawlPolicy,
  listCrawls,
} from '../../lib/api/endpoints';
import { queryKeys } from '../../lib/api/queryKeys';
import type {
  CrawlJobDetail,
  CrawlJobList,
  CrawlJobSummary,
  CreateCrawlJobRequest,
} from '../../lib/api/types';
import { isCrawlActive, jobRefetchInterval, progressSignature, stallWindowMs } from './status';

export function useCrawlPolicy() {
  return useQuery({
    queryKey: queryKeys.crawlPolicy,
    queryFn: ({ signal }) => getCrawlPolicy(signal),
    staleTime: 5 * 60_000,
  });
}

/**
 * Whether this deployment lets the dashboard create and cancel crawl jobs: only when the
 * policy says writes are `open`. A policy not read yet, or unreadable, counts as no. Hiding
 * what the server would refuse is a convenience; the API enforces it (ADR-0024).
 */
export function useCanWrite(): boolean {
  return useCrawlPolicy().data?.write_access === 'open';
}

function listInterval(list: CrawlJobList | undefined, baseMs: number | null): number | false {
  const active = list?.items.some((job) => isCrawlActive(job.status)) ?? false;
  return active && baseMs !== null ? Math.max(baseMs * 2, 5000) : false;
}

/** One page of the job listing (newest first). Refreshed while one of its jobs is active, at
 * twice the base interval (at least 5 s): rows refresh their own progress. */
export function useCrawlList(limit: number, cursor: string | null) {
  const { pollIntervalMs } = useSettings();
  const watch = useStallWatch();
  const query = useQuery({
    queryKey: queryKeys.crawlList(limit, cursor),
    queryFn: ({ signal }) => listCrawls({ limit, cursor }, signal),
    placeholderData: keepPreviousData,
    refetchInterval: (current) =>
      watch.stalled ? false : listInterval(current.state.data, pollIntervalMs),
  });
  const list = query.data;
  watch.observe(
    list === undefined ? null : JSON.stringify(list),
    query.dataUpdatedAt,
    stallReads(5 * 60_000, listInterval(list, pollIntervalMs)),
  );
  return query;
}

export interface LiveCrawlJob {
  query: UseQueryResult<CrawlJobDetail>;
  /** Milliseconds between refreshes, or false when the job is not refreshed. */
  cadence: number | false;
  /** Polling paused: no change over `stallWindowMs` worth of reads. */
  stalled: boolean;
  resume: () => void;
}

/** A job's detail, refreshed while it can change. `factor` slows down secondary views. */
export function useLiveCrawlJob(jobId: string | undefined, factor = 1): LiveCrawlJob {
  const { pollIntervalMs } = useSettings();
  const watch = useStallWatch();

  const query = useQuery({
    queryKey: queryKeys.crawl(jobId ?? ''),
    queryFn: ({ signal }) => getCrawl(jobId ?? '', signal),
    enabled: jobId !== undefined,
    refetchInterval: (current) =>
      watch.stalled ? false : jobRefetchInterval(current.state.data, pollIntervalMs, factor),
  });

  const job = query.data;
  const interval = jobRefetchInterval(job, pollIntervalMs, factor);
  watch.observe(
    job === undefined ? null : progressSignature(job),
    query.dataUpdatedAt,
    job === undefined ? Number.POSITIVE_INFINITY : stallReads(stallWindowMs(job), interval),
  );

  const { refetch } = query;
  const { resume: resumeWatch } = watch;
  const resume = useCallback(() => {
    resumeWatch();
    void refetch();
  }, [resumeWatch, refetch]);

  return { query, cadence: watch.stalled ? false : interval, stalled: watch.stalled, resume };
}

export function useCreateCrawl() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      body: CreateCrawlJobRequest;
      idempotencyKey: string;
    }) => createCrawl(body, idempotencyKey),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.crawls });
      void client.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}

/** Merge a confirmed job summary (the cancel response) into a cached detail, unless the cache
 * already holds a newer state of the job. Progress stays until the detail is read again. */
export function mergeSummary(detail: CrawlJobDetail, summary: CrawlJobSummary): CrawlJobDetail {
  return Date.parse(summary.updated_at) >= Date.parse(detail.updated_at)
    ? { ...detail, ...summary }
    : detail;
}

export function useCancelCrawl(jobId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => cancelCrawl(jobId),
    onSuccess: ({ job }) => {
      // The response is the server's confirmed state, not a guess.
      client.setQueryData<CrawlJobDetail>(queryKeys.crawl(job.id), (current) =>
        current === undefined ? current : mergeSummary(current, job),
      );
      void client.invalidateQueries({ queryKey: queryKeys.crawl(job.id) });
      void client.invalidateQueries({ queryKey: [...queryKeys.crawls, 'list'] });
      void client.invalidateQueries({ queryKey: queryKeys.overview });
    },
  });
}
