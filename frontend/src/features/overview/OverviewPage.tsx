import { ArrowRight } from 'lucide-react';
import { Link } from 'react-router';

import { Button } from '../../components/Button';
import { buttonClasses } from '../../components/buttonClasses';
import { Card } from '../../components/Card';
import { GroupedColumnChart, HorizontalBarChart } from '../../components/charts/Bars';
import { ErrorState, LoadingState, Notice } from '../../components/feedback';
import { SegmentedBar } from '../../components/Meter';
import { StatGrid, StatTile } from '../../components/StatTile';
import type { CrawlJobSummary, OverviewResponse } from '../../lib/api/types';
import { formatDateTime, formatNumber, formatPercent, hostOf, shortId } from '../../lib/format';
import { useCrawlStats } from '../analysis/hooks';
import { useCrawlList } from '../crawls/hooks';
import { JobsTable } from '../crawls/JobsTable';
import { JOB_STATUS, JOB_STATUSES } from '../crawls/status';
import { JobStatusBadge } from '../crawls/StatusBadges';
import { useOverview } from './hooks';

export function OverviewPage() {
  const { query, stalled, resume } = useOverview();

  return (
    <div className="space-y-6">
      {query.isPending ? (
        <LoadingState label="Counting jobs, URLs, pages and documents…" />
      ) : query.isError && query.data === undefined ? (
        <ErrorState
          error={query.error}
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      ) : (
        <SystemSummary
          overview={query.data}
          stalled={stalled}
          onResume={resume}
          refreshError={query.isError ? query.error : null}
        />
      )}
      <LatestJob />
      <Card
        title="Recent crawl jobs"
        description="The eight newest jobs. Running ones refresh by themselves."
        actions={
          <Link to="/crawls" className={buttonClasses('ghost', 'sm')}>
            All jobs <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        }
      >
        <JobsTable pageSize={8} compact />
      </Card>
    </div>
  );
}

interface SystemSummaryProps {
  overview: OverviewResponse;
  stalled: boolean;
  onResume: () => void;
  refreshError: unknown;
}

function SystemSummary({ overview, stalled, onResume, refreshError }: SystemSummaryProps) {
  const { jobs, urls, documents } = overview;
  const active = jobs.by_status.queued + jobs.by_status.running + jobs.by_status.cancelling;
  return (
    <>
      {refreshError !== null && (
        <ErrorState
          error={refreshError}
          title="Refreshing the overview failed; showing the last counts read"
        />
      )}
      {stalled && (
        <Notice
          tone="warning"
          role="status"
          title="Auto-refresh paused"
          action={
            <Button size="sm" onClick={onResume}>
              Resume
            </Button>
          }
        >
          Work remains, but no count changed for three minutes. Check that the scheduler, the
          crawler workers and the indexers are running.
        </Notice>
      )}
      <section aria-labelledby="system-heading" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="system-heading" className="text-sm font-semibold text-ink">
            All crawl jobs
          </h2>
          <p className="text-xs text-ink-3">
            Counted in one PostgreSQL snapshot at {formatDateTime(overview.counted_at)}.
          </p>
        </div>
        <StatGrid>
          <StatTile
            label="Crawl jobs"
            value={formatNumber(jobs.total)}
            hint={`${formatNumber(jobs.by_status.cancelled)} cancelled`}
          />
          <StatTile
            label="Active jobs"
            value={formatNumber(active)}
            tone={active > 0 ? 'info' : undefined}
            hint={`${formatNumber(jobs.by_status.running)} running, ${formatNumber(jobs.by_status.queued)} queued, ${formatNumber(jobs.by_status.cancelling)} cancelling`}
          />
          <StatTile
            label="Completed jobs"
            value={formatNumber(jobs.by_status.completed)}
            tone="good"
            hint="The crawl finished"
          />
          <StatTile
            label="Failed jobs"
            value={formatNumber(jobs.by_status.failed)}
            tone={jobs.by_status.failed > 0 ? 'critical' : undefined}
            hint="The crawl stopped on an error"
          />
          <StatTile
            label="URLs admitted"
            value={formatNumber(urls.total)}
            hint="Seeds included; discovered URLs are counted per job"
          />
          <StatTile
            label="Failed URLs"
            value={formatNumber(urls.by_state.failed)}
            tone={urls.by_state.failed > 0 ? 'critical' : undefined}
            hint={`${formatNumber(urls.by_state.skipped)} skipped by robots.txt or policy`}
          />
          <StatTile
            label="Pages stored"
            value={formatNumber(overview.pages_stored)}
            hint="Fetched 2xx HTML pages"
          />
          <StatTile
            label="Indexed documents"
            value={formatNumber(documents.by_status.indexed)}
            hint={`${formatPercent(documents.by_status.indexed, documents.total)} of ${formatNumber(documents.total)} searchable documents`}
          />
        </StatGrid>
      </section>
      <div className="grid items-start gap-6 xl:grid-cols-2">
        <Card title="Crawl status distribution" description="Every job by its crawl status.">
          <HorizontalBarChart
            caption="Crawl jobs by status"
            categoryName="Crawl status"
            valueName="Jobs"
            data={JOB_STATUSES.map((status) => ({
              label: JOB_STATUS[status].label,
              value: jobs.by_status[status],
            }))}
          />
        </Card>
        <Card
          title="Indexing progress"
          description="Search documents of every job (one per stored page, noindex pages excluded)."
        >
          <SegmentedBar
            label="Search documents by status"
            segments={[
              {
                key: 'indexed',
                label: 'Indexed',
                value: documents.by_status.indexed,
                colorClassName: 'bg-good',
              },
              {
                key: 'queued',
                label: 'Queued',
                value: documents.by_status.queued,
                colorClassName: 'bg-accent',
              },
              {
                key: 'pending',
                label: 'Pending',
                value: documents.by_status.pending,
                colorClassName: 'bg-neutral',
              },
              {
                key: 'failed',
                label: 'Failed',
                value: documents.by_status.failed,
                colorClassName: 'bg-critical',
              },
            ]}
          />
          <p className="mt-4 text-xs text-ink-3">
            {formatNumber(documents.active)} documents are being indexed right now. Pending
            documents wait for the index dispatcher; queued ones for an indexer.
          </p>
        </Card>
      </div>
    </>
  );
}

/** The HTTP and depth distributions of the newest job (one page of the listing, one item). */
function LatestJob() {
  const recent = useCrawlList(1, null);
  const newest: CrawlJobSummary | undefined = recent.data?.items[0];
  if (newest === undefined) return null;
  return <JobDistributions job={newest} />;
}

function JobDistributions({ job }: { job: CrawlJobSummary }) {
  const stats = useCrawlStats(job.id);
  const title = (
    <span className="flex flex-wrap items-center gap-2">
      Newest job: <span className="font-mono text-xs">{shortId(job.id)}</span>
      <JobStatusBadge status={job.status} />
    </span>
  );
  if (stats.isPending) return <LoadingState label="Loading the newest job's statistics…" />;
  if (stats.isError) {
    return (
      <Card title={title}>
        <ErrorState
          error={stats.error}
          onRetry={() => void stats.refetch()}
          retrying={stats.isFetching}
        />
      </Card>
    );
  }
  const data = stats.data;
  const http = data.http_status;
  return (
    <Card
      title={title}
      description={`${hostOf(job.seed_url)} · ${formatNumber(data.crawl.urls_admitted)} URLs admitted · ${formatNumber(data.crawl.pages_stored)} pages stored`}
      actions={
        <Link to={`/analysis?job=${data.job_id}`} className={buttonClasses('ghost', 'sm')}>
          Site analysis <ArrowRight aria-hidden="true" className="size-3.5" />
        </Link>
      }
    >
      <div className="grid gap-6 xl:grid-cols-2">
        <div>
          <h3 className="mb-2 text-xs font-medium text-ink-3">
            HTTP response distribution (URLs by final status)
          </h3>
          <HorizontalBarChart
            caption="URLs of this job by final HTTP status class"
            categoryName="Status class"
            valueName="URLs"
            data={[
              { label: '1xx informational', value: http.informational_1xx },
              { label: '2xx success', value: http.success_2xx },
              { label: '3xx redirect', value: http.redirect_3xx },
              { label: '4xx client error', value: http.client_error_4xx },
              { label: '5xx server error', value: http.server_error_5xx },
              { label: 'No response', value: http.no_response },
            ]}
          />
        </div>
        <div>
          <h3 className="mb-2 text-xs font-medium text-ink-3">BFS depth distribution</h3>
          {data.depths.length === 0 ? (
            <p className="text-sm text-ink-3">No URL admitted yet.</p>
          ) : (
            <GroupedColumnChart
              caption="URLs admitted, fetched and stored as pages, by BFS depth"
              categoryName="Depth"
              categories={data.depths.map((row) => `Depth ${String(row.depth)}`)}
              series={[
                { name: 'URLs admitted', values: data.depths.map((row) => row.urls) },
                { name: 'URLs fetched', values: data.depths.map((row) => row.fetched) },
                { name: 'Pages stored', values: data.depths.map((row) => row.pages) },
              ]}
            />
          )}
        </div>
      </div>
    </Card>
  );
}
