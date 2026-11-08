import { FileText } from 'lucide-react';
import { Link, useSearchParams } from 'react-router';

import { Badge } from '../../components/Badge';
import { buttonClasses } from '../../components/buttonClasses';
import { EmptyState, ErrorState, LoadingState } from '../../components/feedback';
import { Select } from '../../components/Field';
import { CursorPager } from '../../components/Pager';
import { Table, Td, Th, Tr } from '../../components/Table';
import type { Tone } from '../../components/tones';
import { useCursorPager } from '../../hooks/useCursorPager';
import type { PageIndexStatus, PagesQuery } from '../../lib/api/types';
import { formatDateTime, formatNumber, pathOf } from '../../lib/format';
import { ANALYSIS_PAGE_SIZE, PAGE_INDEX_STATUSES, parseDepth, parseIndexStatus } from './filters';
import { useCrawlPages } from './hooks';

const INDEX_STATUS: Record<PageIndexStatus, { label: string; tone: Tone }> = {
  pending: { label: 'Pending', tone: 'neutral' },
  queued: { label: 'Queued', tone: 'info' },
  indexed: { label: 'Indexed', tone: 'good' },
  failed: { label: 'Failed', tone: 'critical' },
  noindex: { label: 'noindex', tone: 'neutral' },
};

/** The job's stored HTML pages, in BFS order (depth, then URL), with cursor paging. */
export function PagesPanel({
  jobId,
  maxDepth,
  linksOf,
}: {
  jobId: string;
  maxDepth: number;
  linksOf: (pageId: string) => string;
}) {
  const [params, setParams] = useSearchParams();
  const depth = parseDepth(params.get('depth'));
  const indexStatus = parseIndexStatus(params.get('index'));
  const pager = useCursorPager(JSON.stringify([jobId, depth, indexStatus]));
  const query: PagesQuery = {
    limit: ANALYSIS_PAGE_SIZE,
    cursor: pager.cursor,
    depth,
    index_status: indexStatus,
  };
  const pages = useCrawlPages(jobId, query);

  function setFilter(name: string, value: string) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value === '') next.delete(name);
      else next.set(name, value);
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <label htmlFor="pages-depth" className="block text-xs font-medium text-ink-2">
            BFS depth
          </label>
          <Select
            id="pages-depth"
            value={depth ?? ''}
            onChange={(event) => {
              setFilter('depth', event.target.value);
            }}
            className="w-32"
          >
            <option value="">Any depth</option>
            {Array.from({ length: maxDepth + 1 }, (_, value) => (
              <option key={value} value={value}>
                Depth {value}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="pages-index" className="block text-xs font-medium text-ink-2">
            Index status
          </label>
          <Select
            id="pages-index"
            value={indexStatus ?? ''}
            onChange={(event) => {
              setFilter('index', event.target.value);
            }}
            className="w-36"
          >
            <option value="">Any status</option>
            {PAGE_INDEX_STATUSES.map((status) => (
              <option key={status} value={status}>
                {INDEX_STATUS[status].label}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {pages.isPending ? (
        <LoadingState label="Loading pages…" />
      ) : pages.isError ? (
        <ErrorState
          error={pages.error}
          onRetry={() => void pages.refetch()}
          retrying={pages.isFetching}
        />
      ) : pages.data.items.length === 0 ? (
        <EmptyState icon={<FileText className="size-5" />} title="No stored pages match">
          A page is a fetched 2xx HTML response the crawl stored; redirects, errors and other
          content types have none.
        </EmptyState>
      ) : (
        <div>
          <Table caption="Stored pages in BFS order" dimmed={pages.isPlaceholderData}>
            <thead>
              <tr>
                <Th>Page</Th>
                <Th className="text-right">Depth</Th>
                <Th className="text-right">HTTP</Th>
                <Th>Index</Th>
                <Th>Crawled</Th>
                <Th>
                  <span className="sr-only">Actions</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {pages.data.items.map((page) => (
                <Tr key={page.page_id}>
                  <Td className="max-w-lg">
                    <p className="truncate text-ink" title={page.title ?? undefined}>
                      {page.title ?? <span className="text-ink-3">(no title)</span>}
                    </p>
                    <p className="truncate font-mono text-xs text-ink-3" title={page.url}>
                      {pathOf(page.url)}
                    </p>
                  </Td>
                  <Td className="text-right tabular-nums">{formatNumber(page.depth)}</Td>
                  <Td className="text-right tabular-nums">{page.http_status}</Td>
                  <Td>
                    <Badge tone={INDEX_STATUS[page.index_status].tone}>
                      {INDEX_STATUS[page.index_status].label}
                    </Badge>
                    {page.nofollow && <span className="ml-1.5 text-xs text-ink-3">nofollow</span>}
                  </Td>
                  <Td className="text-xs whitespace-nowrap">{formatDateTime(page.crawled_at)}</Td>
                  <Td className="text-right">
                    <Link to={linksOf(page.page_id)} className={buttonClasses('ghost', 'sm')}>
                      Links
                    </Link>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          <CursorPager
            label="Page list pages"
            page={pager.page}
            hasPrevious={pager.hasPrevious}
            hasNext={pages.data.next_cursor !== null}
            onPrevious={pager.previous}
            onNext={() => {
              if (pages.data.next_cursor !== null) pager.next(pages.data.next_cursor);
            }}
            loading={pages.isFetching && pages.isPlaceholderData}
          />
        </div>
      )}
    </div>
  );
}
