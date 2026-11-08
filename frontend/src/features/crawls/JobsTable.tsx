import { ListChecks } from 'lucide-react';
import { Link } from 'react-router';

import { buttonClasses } from '../../components/buttonClasses';
import { EmptyState, ErrorState, LoadingState } from '../../components/feedback';
import { Select } from '../../components/Field';
import { CursorPager } from '../../components/Pager';
import { Spinner } from '../../components/Spinner';
import { Table, Td, Th, Tr } from '../../components/Table';
import { useCursorPager } from '../../hooks/useCursorPager';
import { useNow } from '../../hooks/useNow';
import type { CrawlJobSummary } from '../../lib/api/types';
import { formatDateTime, formatNumber, formatRelative, hostOf, shortId } from '../../lib/format';
import { useCrawlList, useLiveCrawlJob } from './hooks';
import { newer } from './status';
import { IndexingBadge, JobStatusBadge } from './StatusBadges';

const PAGE_SIZES = [10, 20, 50] as const;

interface JobsTableProps {
  pageSize: number;
  onPageSizeChange?: (size: number) => void;
  /** Overview mode: the most recent jobs only, no paging, fewer columns. */
  compact?: boolean;
}

/** Crawl jobs, newest first, one page at a time. Each row reads its job's detail (the single
 * source of progress) and refreshes it only while the job can change. */
export function JobsTable({ pageSize, onPageSizeChange, compact = false }: JobsTableProps) {
  const pager = useCursorPager(String(pageSize));
  const list = useCrawlList(pageSize, pager.cursor);

  if (list.isPending) return <LoadingState label="Loading crawl jobs…" />;
  if (list.isError) {
    return (
      <ErrorState
        error={list.error}
        onRetry={() => void list.refetch()}
        retrying={list.isFetching}
      />
    );
  }
  const { items, next_cursor: nextCursor } = list.data;
  if (items.length === 0 && pager.page === 1) {
    return (
      <EmptyState icon={<ListChecks className="size-5" />} title="No crawl jobs yet">
        {compact ? 'Create one on the Crawl Jobs page.' : 'Create one with the form above.'}
      </EmptyState>
    );
  }

  return (
    <div>
      <Table caption="Crawl jobs, newest first" dimmed={list.isPlaceholderData}>
        <thead>
          <tr>
            <Th>Job</Th>
            <Th>Seed {compact ? 'host' : 'URL'}</Th>
            <Th>Crawl</Th>
            {!compact && <Th className="text-right">Depth</Th>}
            <Th className="text-right" title="URLs admitted to the crawl, seed included">
              Admitted
            </Th>
            <Th className="text-right" title="HTML pages stored">
              Pages
            </Th>
            <Th>Indexing</Th>
            <Th>Created</Th>
            <Th>
              <span className="sr-only">Actions</span>
            </Th>
          </tr>
        </thead>
        <tbody>
          {items.map((job) => (
            <JobRow key={job.id} summary={job} compact={compact} />
          ))}
        </tbody>
      </Table>
      {!compact && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 pt-3">
            <label htmlFor="jobs-page-size" className="text-xs text-ink-3">
              Jobs per page
            </label>
            <Select
              id="jobs-page-size"
              value={pageSize}
              onChange={(event) => onPageSizeChange?.(Number(event.target.value))}
              className="h-8 w-20 text-xs"
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </Select>
          </div>
          <CursorPager
            label="Crawl job pages"
            page={pager.page}
            hasPrevious={pager.hasPrevious}
            hasNext={nextCursor !== null}
            onPrevious={pager.previous}
            onNext={() => {
              if (nextCursor !== null) pager.next(nextCursor);
            }}
            loading={list.isFetching && list.isPlaceholderData}
          />
        </div>
      )}
    </div>
  );
}

function JobRow({ summary, compact }: { summary: CrawlJobSummary; compact: boolean }) {
  const now = useNow(30_000); // "3 minutes ago": no need to re-render every row each second
  const { query } = useLiveCrawlJob(summary.id, 2);
  const detail = query.data;
  // The list and the detail are read at different times: show the newer of the two.
  const job = detail === undefined ? summary : newer(detail, summary);

  return (
    <Tr>
      <Td>
        <Link
          to={`/crawls/${summary.id}`}
          className="font-mono text-xs text-accent-ink hover:underline"
          title={summary.id}
        >
          {shortId(summary.id)}
        </Link>
      </Td>
      <Td className="max-w-72">
        <span className="block truncate font-mono text-xs text-ink" title={summary.seed_url}>
          {compact ? hostOf(summary.seed_url) : summary.seed_url}
        </span>
      </Td>
      <Td>
        <JobStatusBadge status={job.status} />
      </Td>
      {!compact && (
        <Td className="text-right tabular-nums">
          {formatNumber(job.current_level)} / {formatNumber(job.max_depth)}
        </Td>
      )}
      <Td className="text-right tabular-nums">
        {formatNumber(job.admitted_count)}
        <span className="text-ink-3"> / {formatNumber(job.max_pages)}</span>
      </Td>
      <Td className="text-right tabular-nums">
        {detail === undefined ? (
          <RowPending failed={query.isError} />
        ) : (
          formatNumber(detail.progress.pages_stored)
        )}
      </Td>
      <Td>
        {detail === undefined ? (
          <RowPending failed={query.isError} />
        ) : (
          <IndexingBadge status={detail.indexing.status} />
        )}
      </Td>
      <Td className="whitespace-nowrap" title={formatDateTime(summary.created_at)}>
        {formatRelative(summary.created_at, now)}
      </Td>
      <Td className="text-right">
        <Link
          to={`/crawls/${summary.id}`}
          className={buttonClasses('ghost', 'sm')}
          aria-label={`View job ${shortId(summary.id)}`}
        >
          View
        </Link>
      </Td>
    </Tr>
  );
}

function RowPending({ failed }: { failed: boolean }) {
  if (failed) return <span className="text-xs text-ink-3">unavailable</span>;
  return (
    <span className="inline-flex text-ink-3">
      <Spinner size={12} />
      <span className="sr-only">Loading</span>
    </span>
  );
}
