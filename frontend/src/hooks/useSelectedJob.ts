/**
 * The crawl job a page works on: the `job` query parameter, so a view can be shared and
 * bookmarked. The last job viewed is remembered in this browser and used when a job page is
 * opened without one. An ID that is not a UUID is reported, never sent to the API.
 */
import { useCallback, useEffect } from 'react';
import { useSearchParams } from 'react-router';

import { isUuid } from '../lib/urls';

const STORAGE_KEY = 'sitescope.lastJobId';

export function recallJob(): string | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value !== null && isUuid(value) ? value : null;
  } catch {
    return null;
  }
}

function rememberJob(jobId: string | null): void {
  try {
    if (jobId === null) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, jobId);
  } catch {
    // Storage unavailable: nothing is remembered.
  }
}

/** Parameters that belong to one job's view and are dropped when the job changes. */
const PER_JOB_PARAMS = [
  'page',
  'source',
  'class',
  'verified',
  'internal',
  'redirected',
  'depth',
  'index',
  'node',
];

export interface SelectedJob {
  jobId: string | null;
  /** The `job` parameter is present but is not a job ID. */
  invalid: boolean;
  select: (jobId: string | null) => void;
}

export function useSelectedJob({ restore = true }: { restore?: boolean } = {}): SelectedJob {
  const [params, setParams] = useSearchParams();
  const raw = params.get('job');
  const valid = raw !== null && isUuid(raw);
  const jobId = valid ? raw.toLowerCase() : null;

  useEffect(() => {
    if (jobId !== null) rememberJob(jobId);
  }, [jobId]);

  useEffect(() => {
    if (!restore || raw !== null) return;
    const last = recallJob();
    if (last === null) return;
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set('job', last);
        return next;
      },
      { replace: true },
    );
  }, [restore, raw, setParams]);

  const select = useCallback(
    (next: string | null) => {
      // Clearing the selection also forgets the remembered job, or it would be restored.
      if (next === null) rememberJob(null);
      setParams((current) => {
        const updated = new URLSearchParams(current);
        if (next === null) updated.delete('job');
        else updated.set('job', next);
        for (const name of PER_JOB_PARAMS) updated.delete(name);
        return updated;
      });
    },
    [setParams],
  );

  return { jobId, invalid: raw !== null && !valid, select };
}
