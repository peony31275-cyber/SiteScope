/**
 * What a job's crawl status and indexing status mean, and how often to refresh a job.
 *
 * The crawl and its indexing are separate lifecycles (docs/api-conventions.md, "Crawl jobs"):
 * a crawl can be `completed` while its indexing is still `running`. A job is settled only when
 * its crawl has ended AND its indexing is complete (`indexing.completed_at`). Only then does
 * the dashboard stop refreshing it.
 */
import type { Tone } from '../../components/tones';
import type {
  CrawlJobDetail,
  CrawlJobSummary,
  IndexingState,
  JobStatus,
} from '../../lib/api/types';
import { hostOf, shortId } from '../../lib/format';

export const JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'cancelling',
  'completed',
  'failed',
  'cancelled',
];

/** Statuses in which the crawl may still change. */
export const ACTIVE_STATUSES: ReadonlySet<JobStatus> = new Set(['queued', 'running', 'cancelling']);

export function isCrawlActive(status: JobStatus): boolean {
  return ACTIVE_STATUSES.has(status);
}

/** Cancelling makes sense while nothing has ended the crawl yet. */
export function isCancellable(status: JobStatus): boolean {
  return status === 'queued' || status === 'running';
}

export function isSettled(job: CrawlJobDetail): boolean {
  return !isCrawlActive(job.status) && job.indexing.completed_at !== null;
}

interface Meaning {
  label: string;
  tone: Tone;
  description: string;
}

export const JOB_STATUS: Record<JobStatus, Meaning> = {
  queued: {
    label: 'Queued',
    tone: 'neutral',
    description: 'Waiting for the scheduler to start it. Nothing is crawled yet.',
  },
  running: {
    label: 'Running',
    tone: 'info',
    description: 'The crawl is fetching URLs level by level (BFS).',
  },
  cancelling: {
    label: 'Cancelling',
    tone: 'warning',
    description:
      'Cancellation is settling: nothing new is dispatched, and the URLs in flight are finishing.',
  },
  completed: {
    label: 'Completed',
    tone: 'good',
    description: 'The crawl finished every admitted URL.',
  },
  failed: {
    label: 'Failed',
    tone: 'critical',
    description: 'The crawl stopped because of an error.',
  },
  cancelled: {
    label: 'Cancelled',
    tone: 'neutral',
    description: 'The crawl was cancelled. Pages already stored are kept.',
  },
};

export const INDEXING_STATUS: Record<IndexingState, Meaning> = {
  pending: {
    label: 'Indexing pending',
    tone: 'neutral',
    description: 'No page has been published to the indexers yet.',
  },
  running: {
    label: 'Indexing running',
    tone: 'info',
    description: 'Pages are being indexed, or wait for the crawl or the index dispatcher.',
  },
  indexed: {
    label: 'Fully indexed',
    tone: 'good',
    description: 'The crawl ended and every indexable page is searchable.',
  },
  failed: {
    label: 'Indexing failed',
    tone: 'critical',
    description:
      'Indexing is over, but some pages could not be indexed. The others are searchable.',
  },
};

/** One sentence for the state of the whole job. */
export function lifecycleSummary(job: CrawlJobDetail): string {
  const { indexing } = job;
  const docs = `${String(indexing.documents_by_status.indexed)} of ${String(indexing.documents_total)} pages indexed`;
  switch (job.status) {
    case 'queued':
      return 'Queued: waiting for the scheduler to start the crawl.';
    case 'running':
      return `Crawling depth ${String(job.current_level)} of ${String(job.max_depth)}; ${docs} so far.`;
    case 'cancelling':
      return `Cancelling: ${String(job.progress.urls_by_state.in_progress)} URLs in flight are finishing.`;
    case 'completed':
    case 'failed':
    case 'cancelled': {
      const crawl = JOB_STATUS[job.status].label;
      if (indexing.status === 'indexed')
        return `Crawl ${crawl.toLowerCase()} · fully indexed (${docs}).`;
      if (indexing.status === 'failed') {
        return `Crawl ${crawl.toLowerCase()} · indexing failed for ${String(indexing.documents_by_status.failed)} pages (${docs}).`;
      }
      return `Crawl ${crawl.toLowerCase()} · indexing not complete yet (${docs}).`;
    }
  }
}

/** Refresh cadence of a job, given the viewer's base interval (null: auto-refresh off).
 * Running crawls: the base interval. Crawl ended, indexing not complete: slower (at least
 * twice the base, and 5 s). Settled: never. `factor` slows down secondary views (table rows). */
export function jobRefetchInterval(
  job: CrawlJobDetail | undefined,
  baseMs: number | null,
  factor = 1,
): number | false {
  if (job === undefined || baseMs === null || isSettled(job)) return false;
  const interval = isCrawlActive(job.status) ? baseMs : Math.max(baseMs * 2, 5000);
  return interval * factor;
}

/** How much watching without change pauses auto-refresh (as a number of reads, so time in a
 * hidden tab does not count; `stallReads`): a queued or running crawl that does not move for 5
 * minutes of reads, or indexing that does not move for 3, probably waits for a process that is
 * not running (scheduler, crawler worker, index dispatcher, indexer). */
export function stallWindowMs(job: CrawlJobDetail): number {
  return isCrawlActive(job.status) ? 5 * 60_000 : 3 * 60_000;
}

/** Everything about a job that can change while it runs. Equal signatures: no progress. */
export function progressSignature(job: CrawlJobDetail): string {
  return JSON.stringify([
    job.status,
    job.updated_at,
    job.current_level,
    job.admitted_count,
    job.progress.urls_by_state,
    job.progress.pages_stored,
    job.indexing.documents_by_status,
    job.indexing.active,
    job.indexing.completed_at,
  ]);
}

/** The newer of two views of one job (a list row and its detail, or a cancel response and the
 * cached detail), by the job row's `updated_at`; a tie keeps the first. */
export function newer<T extends CrawlJobSummary>(
  current: T,
  candidate: CrawlJobSummary,
): T | CrawlJobSummary {
  return Date.parse(candidate.updated_at) > Date.parse(current.updated_at) ? candidate : current;
}

/** A job in one line: short ID, seed host and crawl status. */
export function jobOptionLabel(job: Pick<CrawlJobSummary, 'id' | 'seed_url' | 'status'>): string {
  return `${shortId(job.id)} · ${hostOf(job.seed_url)} · ${JOB_STATUS[job.status].label}`;
}
