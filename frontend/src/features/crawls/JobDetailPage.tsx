import { ChartColumn, Network, Search } from 'lucide-react';
import { useRef, type ReactNode } from 'react';
import { Link, useParams } from 'react-router';

import { Button, ButtonLink } from '../../components/Button';
import { buttonClasses } from '../../components/buttonClasses';
import { Card } from '../../components/Card';
import { CopyButton } from '../../components/CopyButton';
import { ExternalUrl } from '../../components/ExternalUrl';
import { EmptyState, ErrorState, LoadingState, Notice } from '../../components/feedback';
import { Meter, SegmentedBar } from '../../components/Meter';
import { StatGrid, StatTile } from '../../components/StatTile';
import { useNow } from '../../hooks/useNow';
import { describeError, isNotFound } from '../../lib/api/errors';
import type { CrawlJobDetail, UrlState } from '../../lib/api/types';
import { formatDateTime, formatDuration, formatNumber, formatRelative } from '../../lib/format';
import { isUuid } from '../../lib/urls';
import { CancelJobButton } from './CancelJobButton';
import { useCanWrite, useLiveCrawlJob, type LiveCrawlJob } from './hooks';
import { isCrawlActive, lifecycleSummary } from './status';
import { IndexingBadge, JobStatusBadge } from './StatusBadges';

export function JobDetailPage() {
  const { jobId = '' } = useParams();
  if (!isUuid(jobId)) {
    return (
      <EmptyState
        title="This is not a crawl job ID"
        action={
          <Link to="/crawls" className={buttonClasses('primary')}>
            Go to the crawl jobs
          </Link>
        }
      >
        Job IDs are UUIDs, such as 0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b.
      </EmptyState>
    );
  }
  // One component tree per job: moving to another job (the browser's back button, a link)
  // starts afresh, so no state of this job (an open dialog, a pending action) carries over.
  const id = jobId.toLowerCase();
  return <JobDetail key={id} jobId={id} />;
}

function JobDetail({ jobId }: { jobId: string }) {
  const live = useLiveCrawlJob(jobId);
  const { query } = live;

  if (query.isPending) return <LoadingState label="Loading the crawl job…" />;
  if (query.isError && query.data === undefined) {
    if (isNotFound(query.error)) {
      return (
        <EmptyState
          title="Crawl job not found"
          action={
            <Link to="/crawls" className={buttonClasses('primary')}>
              Go to the crawl jobs
            </Link>
          }
        >
          No crawl job has the ID <span className="font-mono">{jobId}</span>.
        </EmptyState>
      );
    }
    return (
      <ErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    );
  }
  const job = query.data;

  return (
    <div className="space-y-6">
      <JobHeader job={job} live={live} />
      <JobNotices job={job} live={live} />
      <StatGrid className="xl:grid-cols-6">
        <StatTile
          label="URLs admitted"
          value={formatNumber(job.admitted_count)}
          hint={`of ${formatNumber(job.max_pages)} allowed`}
        />
        <StatTile
          label="URLs finished"
          value={formatNumber(job.progress.urls_finished)}
          hint={`of ${formatNumber(job.progress.urls_total)} admitted`}
        />
        <StatTile
          label="Pages stored"
          value={formatNumber(job.progress.pages_stored)}
          hint="HTML pages (2xx)"
        />
        <StatTile
          label="BFS depth"
          value={formatNumber(job.current_level)}
          hint={`of ${formatNumber(job.max_depth)} allowed`}
        />
        <StatTile
          label="Indexed pages"
          value={formatNumber(job.indexing.documents_by_status.indexed)}
          hint={`of ${formatNumber(job.indexing.documents_total)} to index`}
        />
        <StatTile
          label="Failed URLs"
          value={formatNumber(job.progress.urls_by_state.failed)}
          tone={job.progress.urls_by_state.failed > 0 ? 'critical' : undefined}
          hint={`${formatNumber(job.progress.urls_by_state.skipped)} skipped by policy`}
        />
      </StatGrid>
      <div className="grid items-start gap-6 xl:grid-cols-2">
        <CrawlProgressCard job={job} />
        <IndexingCard job={job} />
        <ConfigurationCard job={job} />
        <TimelineCard job={job} />
      </div>
    </div>
  );
}

function JobHeader({ job, live }: { job: CrawlJobDetail; live: LiveCrawlJob }) {
  const now = useNow();
  const canWrite = useCanWrite();
  // Where focus goes once the job was cancelled: the cancel button is gone then.
  const summaryRef = useRef<HTMLParagraphElement>(null);
  const updated = live.query.dataUpdatedAt;
  const scoped = `?job=${job.id}`;
  return (
    <section aria-label="Crawl job" className="rounded-xl border border-line bg-surface p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <JobStatusBadge status={job.status} />
            <IndexingBadge status={job.indexing.status} />
          </div>
          <div className="flex items-center gap-1.5">
            <p className="font-mono text-sm text-ink">{job.id}</p>
            <CopyButton value={job.id} label="Copy job ID" />
          </div>
          <p className="text-sm">
            <ExternalUrl url={job.seed_url} className="font-mono text-xs" />
          </p>
          <p ref={summaryRef} tabIndex={-1} className="text-sm text-ink-2" aria-live="polite">
            {lifecycleSummary(job)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ButtonLink
            to={`/search${scoped}`}
            size="sm"
            icon={<Search aria-hidden="true" className="size-3.5" />}
          >
            Search
          </ButtonLink>
          <ButtonLink
            to={`/analysis${scoped}`}
            size="sm"
            icon={<ChartColumn aria-hidden="true" className="size-3.5" />}
          >
            Analysis
          </ButtonLink>
          <ButtonLink
            to={`/graph${scoped}`}
            size="sm"
            icon={<Network aria-hidden="true" className="size-3.5" />}
          >
            Link graph
          </ButtonLink>
          {canWrite && (
            <CancelJobButton
              job={job}
              onCancelled={() => {
                summaryRef.current?.focus();
              }}
            />
          )}
        </div>
      </div>
      <p className="mt-3 border-t border-line pt-3 text-xs text-ink-3">
        {live.cadence === false
          ? live.stalled
            ? 'Auto-refresh paused.'
            : isCrawlActive(job.status) || job.indexing.completed_at === null
              ? 'Auto-refresh is off.'
              : 'Final state: the crawl and its indexing are over, so this page no longer refreshes.'
          : `Refreshing every ${String(live.cadence / 1000)} s.`}{' '}
        Last read {formatRelative(new Date(updated).toISOString(), now)} (
        {formatDateTime(new Date(updated).toISOString())}).
      </p>
    </section>
  );
}

function JobNotices({ job, live }: { job: CrawlJobDetail; live: LiveCrawlJob }) {
  const { query } = live;
  return (
    <>
      {query.isError && (
        <Notice
          tone="danger"
          role="alert"
          title={`Refresh failed: ${describeError(query.error).title}`}
        >
          The page shows the last state read from the server.{' '}
          {describeError(query.error).transient && 'It retries automatically.'}
        </Notice>
      )}
      {job.status === 'cancelling' && (
        <Notice tone="warning" role="status" title="Cancellation is settling">
          Nothing new is dispatched, and waiting URLs were cancelled.{' '}
          {formatNumber(job.progress.urls_by_state.in_progress)} URL
          {job.progress.urls_by_state.in_progress === 1 ? ' is' : 's are'} still being fetched:
          workers stop them within seconds, and the URLs of a crashed worker are cancelled when
          their lease expires. The job becomes cancelled then.
        </Notice>
      )}
      {job.status === 'failed' && (
        <Notice tone="danger" title="The crawl failed">
          {job.failure_reason ?? 'The server recorded no reason.'}
        </Notice>
      )}
      {job.status === 'queued' && (
        <Notice tone="info" title="Waiting for the scheduler">
          A job stays queued until a scheduler process starts it; nothing is crawled before then.
        </Notice>
      )}
      {live.stalled && (
        <Notice
          tone="warning"
          role="status"
          title="Auto-refresh paused: no change for several minutes"
          action={
            <Button size="sm" onClick={live.resume}>
              Resume
            </Button>
          }
        >
          {isCrawlActive(job.status)
            ? 'The crawl has not moved. Check that the scheduler and the crawler workers are running.'
            : 'Indexing has not moved. Check that the index dispatcher and the indexers are running.'}
        </Notice>
      )}
    </>
  );
}

const URL_STATES: readonly { state: UrlState; label: string; description: string }[] = [
  { state: 'pending', label: 'Pending', description: 'admitted, waiting to be dispatched' },
  { state: 'queued', label: 'Queued', description: 'published to the crawler workers' },
  { state: 'in_progress', label: 'In progress', description: 'being fetched under a lease' },
  { state: 'fetched', label: 'Fetched', description: 'a response was recorded (any status)' },
  { state: 'failed', label: 'Failed', description: 'no usable response after bounded retries' },
  { state: 'skipped', label: 'Skipped', description: 'not requested: robots.txt or policy' },
  { state: 'cancelled', label: 'Cancelled', description: 'the job was cancelled first' },
];

function CrawlProgressCard({ job }: { job: CrawlJobDetail }) {
  const { progress } = job;
  return (
    <Card
      title="Crawl progress"
      description="Counted from the job's URL rows in PostgreSQL when the page was read."
    >
      <div className="space-y-4">
        <Progress label="URLs finished" value={progress.urls_finished} max={progress.urls_total} />
        <Progress label="Page budget used" value={job.admitted_count} max={job.max_pages} />
        <div>
          <h3 className="mb-2 text-xs font-medium text-ink-3">Admitted URLs by state</h3>
          <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            {URL_STATES.map(({ state, label, description }) => (
              <div
                key={state}
                className="flex items-baseline justify-between gap-3 border-b border-line/50 py-1"
              >
                <dt className="text-sm text-ink-2" title={description}>
                  {label}
                </dt>
                <dd className="text-sm font-medium text-ink tabular-nums">
                  {formatNumber(progress.urls_by_state[state])}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </Card>
  );
}

function Progress({ label, value, max }: { label: string; value: number; max: number }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-ink-2">{label}</span>
        <span className="text-ink tabular-nums">
          {formatNumber(value)} <span className="text-ink-3">/ {formatNumber(max)}</span>
        </span>
      </div>
      <Meter label={label} value={value} max={max} />
    </div>
  );
}

function IndexingCard({ job }: { job: CrawlJobDetail }) {
  const { indexing } = job;
  const counts = indexing.documents_by_status;
  return (
    <Card
      title="Indexing"
      description="Search documents: every stored page except noindex ones. Indexing has its own lifecycle."
      actions={<IndexingBadge status={indexing.status} />}
    >
      <div className="space-y-4">
        <SegmentedBar
          label="Search documents by status"
          segments={[
            { key: 'indexed', label: 'Indexed', value: counts.indexed, colorClassName: 'bg-good' },
            { key: 'queued', label: 'Queued', value: counts.queued, colorClassName: 'bg-accent' },
            {
              key: 'pending',
              label: 'Pending',
              value: counts.pending,
              colorClassName: 'bg-neutral',
            },
            { key: 'failed', label: 'Failed', value: counts.failed, colorClassName: 'bg-critical' },
          ]}
        />
        <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
          <Row label="Pages to index">{formatNumber(indexing.documents_total)}</Row>
          <Row label="Being indexed now">{formatNumber(indexing.active)}</Row>
          <Row label="Indexing completed">{formatDateTime(indexing.completed_at)}</Row>
          <Row label="Not indexable (noindex)">
            {formatNumber(Math.max(0, job.progress.pages_stored - indexing.documents_total))}
          </Row>
        </dl>
        {indexing.status === 'failed' && (
          <p className="text-sm text-ink-2">
            {formatNumber(counts.failed)} pages could not be indexed after bounded attempts; the
            other pages are searchable.
          </p>
        )}
      </div>
    </Card>
  );
}

function ConfigurationCard({ job }: { job: CrawlJobDetail }) {
  return (
    <Card title="Configuration" description="As submitted; the seed URL is stored normalized.">
      <dl className="grid gap-y-1.5 text-sm">
        <Row label="Job ID">
          <span className="font-mono text-xs break-all">{job.id}</span>
        </Row>
        <Row label="Seed URL">
          <span className="font-mono text-xs break-all">{job.seed_url}</span>
        </Row>
        <Row label="Allowed hosts">
          <span className="font-mono text-xs">{job.allowed_hosts.join(', ')}</span>
        </Row>
        <Row label="Maximum BFS depth">{formatNumber(job.max_depth)}</Row>
        <Row label="Page limit">{formatNumber(job.max_pages)}</Row>
      </dl>
    </Card>
  );
}

function TimelineCard({ job }: { job: CrawlJobDetail }) {
  const crawlTime =
    job.started_at !== null && job.finished_at !== null
      ? formatDuration(Date.parse(job.finished_at) - Date.parse(job.started_at))
      : null;
  return (
    <Card title="Timeline" description="Server times, shown in your time zone.">
      <dl className="grid gap-y-1.5 text-sm">
        <Row label="Created">{formatDateTime(job.created_at)}</Row>
        <Row label="Started">{formatDateTime(job.started_at)}</Row>
        <Row label="Cancel requested">{formatDateTime(job.cancel_requested_at)}</Row>
        <Row label="Crawl ended">{formatDateTime(job.finished_at)}</Row>
        <Row label="Indexing completed">{formatDateTime(job.indexing.completed_at)}</Row>
        <Row label="Crawl duration">{crawlTime ?? '—'}</Row>
        <Row label="Last change">{formatDateTime(job.updated_at)}</Row>
      </dl>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line/50 py-1">
      <dt className="shrink-0 text-ink-3">{label}</dt>
      <dd className="min-w-0 text-right text-ink tabular-nums">{children}</dd>
    </div>
  );
}
