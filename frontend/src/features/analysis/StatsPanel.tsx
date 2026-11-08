import { Link } from 'react-router';

import { Badge } from '../../components/Badge';
import { Card } from '../../components/Card';
import { GroupedColumnChart, HorizontalBarChart } from '../../components/charts/Bars';
import { ExternalUrl } from '../../components/ExternalUrl';
import { ErrorState, LoadingState, Notice } from '../../components/feedback';
import { SegmentedBar } from '../../components/Meter';
import { StatGrid, StatTile } from '../../components/StatTile';
import { Table, Td, Th, Tr } from '../../components/Table';
import type { CrawlStatsResponse } from '../../lib/api/types';
import { formatNumber, formatPercent, pathOf } from '../../lib/format';
import { isCrawlActive } from '../crawls/status';
import { IndexingBadge, JobStatusBadge } from '../crawls/StatusBadges';
import { useCrawlStats } from './hooks';
import { LINK_CLASS_ORDER, LINK_CLASSES } from './linkClasses';

/** The HTTP status buckets of the API, in order, with their labels. */
const HTTP_BUCKETS = [
  ['informational_1xx', '1xx informational'],
  ['success_2xx', '2xx success'],
  ['redirect_3xx', '3xx redirect'],
  ['client_error_4xx', '4xx client error'],
  ['server_error_5xx', '5xx server error'],
  ['no_response', 'No response'],
] as const;

export function StatsPanel({
  jobId,
  linksHref,
}: {
  jobId: string;
  linksHref: (linkClass: string) => string;
}) {
  const stats = useCrawlStats(jobId);
  if (stats.isPending) return <LoadingState label="Counting the job's statistics…" />;
  if (stats.isError && stats.data === undefined) {
    return (
      <ErrorState
        error={stats.error}
        onRetry={() => void stats.refetch()}
        retrying={stats.isFetching}
      />
    );
  }
  return (
    <Statistics
      stats={stats.data}
      linksHref={linksHref}
      refreshFailed={stats.isError ? stats.error : null}
    />
  );
}

function Statistics({
  stats,
  linksHref,
  refreshFailed,
}: {
  stats: CrawlStatsResponse;
  linksHref: (linkClass: string) => string;
  refreshFailed: unknown;
}) {
  const { crawl, http_status: http, links, indexing } = stats;
  const finished = crawl.urls_by_state.fetched + crawl.urls_by_state.failed;
  const httpErrors = http.client_error_4xx + http.server_error_5xx;

  return (
    <div className="space-y-6">
      {refreshFailed !== null && (
        <ErrorState
          error={refreshFailed}
          title="Refreshing the statistics failed; showing the last ones read"
        />
      )}
      {isCrawlActive(stats.status) && (
        <Notice tone="info" role="status" title="The crawl is still running">
          These numbers are a snapshot: they grow as the crawl continues, and refresh about every
          ten seconds.
        </Notice>
      )}
      <div className="flex flex-wrap items-center gap-2 text-sm text-ink-2">
        <JobStatusBadge status={stats.status} />
        <IndexingBadge status={indexing.status} />
        <span className="text-xs text-ink-3">
          All numbers come from one snapshot of PostgreSQL.
        </span>
      </div>

      <section aria-labelledby="crawl-stats-heading" className="space-y-3">
        <h3 id="crawl-stats-heading" className="text-sm font-semibold text-ink">
          Crawl statistics
        </h3>
        <StatGrid className="xl:grid-cols-4">
          <StatTile
            label="URLs discovered"
            value={formatNumber(crawl.urls_discovered)}
            hint="Admitted, plus internal link targets never admitted"
          />
          <StatTile
            label="URLs admitted"
            value={formatNumber(crawl.urls_admitted)}
            hint={`of ${formatNumber(crawl.max_pages)} allowed`}
          />
          <StatTile
            label="Pages stored"
            value={formatNumber(crawl.pages_stored)}
            hint="Fetched 2xx HTML pages"
          />
          <StatTile
            label="Crawl depth"
            value={formatNumber(crawl.current_depth)}
            hint={`BFS level; at most ${formatNumber(crawl.max_depth)}`}
          />
          <StatTile
            label="Successful responses"
            value={formatNumber(http.success_2xx)}
            tone="good"
            hint={`2xx, of ${formatNumber(finished)} finished requests`}
          />
          <StatTile
            label="HTTP errors"
            value={formatNumber(httpErrors)}
            tone={httpErrors > 0 ? 'warning' : undefined}
            hint={`${formatNumber(http.client_error_4xx)} 4xx, ${formatNumber(http.server_error_5xx)} 5xx`}
          />
          <StatTile
            label="Failed requests"
            value={formatNumber(crawl.urls_by_state.failed)}
            tone={crawl.urls_by_state.failed > 0 ? 'critical' : undefined}
            hint="No usable response after retries"
          />
          <StatTile
            label="Skipped URLs"
            value={formatNumber(crawl.urls_by_state.skipped)}
            hint="Never requested: robots.txt or policy"
          />
        </StatGrid>
      </section>

      <div className="grid items-start gap-6 xl:grid-cols-2">
        <Card
          title="HTTP status distribution"
          description={`The final status of each URL whose request ended (${formatNumber(finished)}); a redirect counts as 3xx.`}
        >
          <HorizontalBarChart
            caption="URLs by final HTTP status class"
            categoryName="Status class"
            valueName="URLs"
            data={HTTP_BUCKETS.map(([key, label]) => ({ label, value: http[key] }))}
          />
        </Card>
        <Card
          title="BFS depth distribution"
          description="Per depth: URLs admitted, URLs fetched (a response recorded), and HTML pages stored."
        >
          {stats.depths.length === 0 ? (
            <p className="text-sm text-ink-3">No URL has been admitted yet.</p>
          ) : (
            <GroupedColumnChart
              caption="URLs admitted, fetched and stored as pages, by BFS depth"
              categoryName="Depth"
              categories={stats.depths.map((row) => `Depth ${String(row.depth)}`)}
              series={[
                { name: 'URLs admitted', values: stats.depths.map((row) => row.urls) },
                { name: 'URLs fetched', values: stats.depths.map((row) => row.fetched) },
                { name: 'Pages stored', values: stats.depths.map((row) => row.pages) },
              ]}
            />
          )}
        </Card>
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-2">
        <Card
          title="Indexing progress"
          description="Search documents: every stored page except noindex ones."
          actions={<IndexingBadge status={indexing.status} />}
        >
          <SegmentedBar
            label="Search documents by status"
            segments={[
              {
                key: 'indexed',
                label: 'Indexed',
                value: indexing.documents_by_status.indexed,
                colorClassName: 'bg-good',
              },
              {
                key: 'queued',
                label: 'Queued',
                value: indexing.documents_by_status.queued,
                colorClassName: 'bg-accent',
              },
              {
                key: 'pending',
                label: 'Pending',
                value: indexing.documents_by_status.pending,
                colorClassName: 'bg-neutral',
              },
              {
                key: 'failed',
                label: 'Failed',
                value: indexing.documents_by_status.failed,
                colorClassName: 'bg-critical',
              },
            ]}
          />
          <p className="mt-3 text-xs text-ink-3">
            {formatNumber(indexing.documents_total)} documents; {formatNumber(indexing.active)}{' '}
            being indexed now.
          </p>
        </Card>
        <LinkClassesCard stats={stats} linksHref={linksHref} />
      </div>

      <Card
        title="Most linked URLs"
        description="By stored internal links to the URL (one per linking page). A URL need not be a stored page."
      >
        {stats.most_linked.length === 0 ? (
          <p className="text-sm text-ink-3">No internal links are stored yet.</p>
        ) : (
          <Table caption="The URLs with the most stored internal links to them">
            <thead>
              <tr>
                <Th>URL</Th>
                <Th className="text-right">Internal links to it</Th>
                <Th>Page</Th>
              </tr>
            </thead>
            <tbody>
              {stats.most_linked.map((item) => (
                <Tr key={item.url}>
                  <Td className="max-w-md">
                    <ExternalUrl
                      url={item.url}
                      label={pathOf(item.url)}
                      className="font-mono text-xs"
                    />
                  </Td>
                  <Td className="text-right text-ink tabular-nums">
                    {formatNumber(item.in_links)}
                  </Td>
                  <Td>
                    {item.page_id === null ? (
                      <span className="text-xs text-ink-3">no stored page</span>
                    ) : (
                      <Badge tone="good">stored</Badge>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {links.total > 0 && (
        <p className="text-xs text-ink-3">
          {formatNumber(links.total)} links: {formatNumber(links.internal)} internal (to{' '}
          {formatNumber(links.internal_targets)} distinct targets), {formatNumber(links.external)}{' '}
          external.
        </p>
      )}
    </div>
  );
}

function LinkClassesCard({
  stats,
  linksHref,
}: {
  stats: CrawlStatsResponse;
  linksHref: (linkClass: string) => string;
}) {
  const { links } = stats;
  const max = Math.max(1, ...Object.values(links.by_class));
  return (
    <Card
      title="Links by classification"
      description={`${formatNumber(links.verified)} verified by an HTTP response, ${formatNumber(links.unverified)} unverified (never requested, or no response).`}
    >
      <Table caption="Stored links by classification">
        <thead>
          <tr>
            <Th>Classification</Th>
            <Th>Verified</Th>
            <Th className="w-1/3">
              <span className="sr-only">Share</span>
            </Th>
            <Th className="text-right">Links</Th>
          </tr>
        </thead>
        <tbody>
          {LINK_CLASS_ORDER.map((linkClass) => {
            const meaning = LINK_CLASSES[linkClass];
            const count = links.by_class[linkClass];
            return (
              <Tr key={linkClass}>
                <Td>
                  <Link
                    to={linksHref(linkClass)}
                    title={meaning.description}
                    className="hover:underline"
                  >
                    <Badge tone={meaning.tone}>{meaning.label}</Badge>
                  </Link>
                </Td>
                <Td className="text-xs">{meaning.verified ? 'Verified' : 'Unverified'}</Td>
                <Td className="align-middle">
                  <div aria-hidden="true" className="h-1.5 rounded-full bg-raised">
                    <div
                      className={
                        meaning.verified
                          ? 'h-full rounded-full bg-accent'
                          : 'h-full rounded-full bg-neutral'
                      }
                      style={{ width: `${String((count / max) * 100)}%` }}
                    />
                  </div>
                </Td>
                <Td className="text-right text-ink tabular-nums">
                  {formatNumber(count)}{' '}
                  <span className="text-xs text-ink-3">{formatPercent(count, links.total)}</span>
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
      <p className="mt-3 text-xs text-ink-3">
        Only <span className="text-ink-2">Broken</span> links are confirmed broken (404 or 410).
        Unverified links are neither working nor broken: the crawl has no response for them.
      </p>
    </Card>
  );
}
