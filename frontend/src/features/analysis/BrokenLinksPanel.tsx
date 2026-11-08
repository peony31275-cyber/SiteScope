import { CircleCheck } from 'lucide-react';
import { Link } from 'react-router';

import { Badge } from '../../components/Badge';
import { ExternalUrl } from '../../components/ExternalUrl';
import { EmptyState, ErrorState, LoadingState, Notice } from '../../components/feedback';
import { CursorPager } from '../../components/Pager';
import { Table, Td, Th, Tr } from '../../components/Table';
import { useCursorPager } from '../../hooks/useCursorPager';
import { formatDateTime, formatNumber, pathOf } from '../../lib/format';
import { ANALYSIS_PAGE_SIZE } from './filters';
import { useCrawlBrokenLinks } from './hooks';

/** Confirmed broken links only (`/broken-links`): internal links whose target, after its
 * redirects through the job's own crawl results, answered 404 or 410 during this crawl. */
export function BrokenLinksPanel({
  jobId,
  unverifiedHref,
}: {
  jobId: string;
  unverifiedHref: string;
}) {
  const pager = useCursorPager(jobId);
  const broken = useCrawlBrokenLinks(jobId, { limit: ANALYSIS_PAGE_SIZE, cursor: pager.cursor });

  return (
    <div className="space-y-4">
      <Notice tone="info" title="Confirmed broken links only">
        A link is listed when it is internal and its target, after its redirects, answered 404 Not
        Found or 410 Gone during this crawl. Server errors, refused requests (401, 403), and links
        the crawl never requested or got no response for are not broken: they are in the{' '}
        <Link to={unverifiedHref} className="text-accent-ink hover:underline">
          links listing
        </Link>
        , by classification.
      </Notice>
      {broken.isPending ? (
        <LoadingState label="Loading broken links…" />
      ) : broken.isError ? (
        <ErrorState
          error={broken.error}
          onRetry={() => void broken.refetch()}
          retrying={broken.isFetching}
        />
      ) : broken.data.total === 0 ? (
        <EmptyState icon={<CircleCheck className="size-5" />} title="No confirmed broken link">
          No internal link of this job led to a 404 or 410. Unverified links are not counted here.
        </EmptyState>
      ) : (
        <div>
          <p className="mb-3 text-sm text-ink-2" aria-live="polite">
            <span className="font-medium text-ink">{formatNumber(broken.data.total)}</span>{' '}
            confirmed broken link
            {broken.data.total === 1 ? '' : 's'} (one per source page and target).
          </p>
          <Table caption="Confirmed broken links" dimmed={broken.isPlaceholderData}>
            <thead>
              <tr>
                <Th>Source page</Th>
                <Th>Anchor text</Th>
                <Th>Target URL</Th>
                <Th>Status</Th>
                <Th>Verification</Th>
              </tr>
            </thead>
            <tbody>
              {broken.data.items.map((link) => (
                <Tr key={`${link.source_page_id}|${link.target_url}`}>
                  <Td className="max-w-60">
                    <ExternalUrl
                      url={link.source_url}
                      label={pathOf(link.source_url)}
                      className="font-mono text-xs"
                    />
                  </Td>
                  <Td className="max-w-48">
                    <span className="block truncate" title={link.anchor_text ?? undefined}>
                      {link.anchor_text ?? <span className="text-ink-3">(none)</span>}
                    </span>
                  </Td>
                  <Td className="max-w-72">
                    <span
                      className="block truncate font-mono text-xs text-ink"
                      title={link.target_url}
                    >
                      {pathOf(link.target_url)}
                    </span>
                  </Td>
                  <Td>
                    <Badge tone="critical">
                      {link.status === 410 ? '410 Gone' : `${String(link.status)} Not Found`}
                    </Badge>
                  </Td>
                  <Td className="text-xs">
                    {link.final_url === null ? (
                      <span>Target answered {link.status}</span>
                    ) : (
                      <span>
                        Redirected {link.redirect_hops} time{link.redirect_hops === 1 ? '' : 's'}{' '}
                        (first {link.target_http_status}) to{' '}
                        <span className="font-mono" title={link.final_url}>
                          {pathOf(link.final_url)}
                        </span>
                      </span>
                    )}
                    <span className="block text-ink-3">
                      checked {formatDateTime(link.checked_at)}
                    </span>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          <CursorPager
            label="Broken link pages"
            page={pager.page}
            hasPrevious={pager.hasPrevious}
            hasNext={broken.data.next_cursor !== null}
            onPrevious={pager.previous}
            onNext={() => {
              if (broken.data.next_cursor !== null) pager.next(broken.data.next_cursor);
            }}
            loading={broken.isFetching && broken.isPlaceholderData}
          />
        </div>
      )}
    </div>
  );
}
