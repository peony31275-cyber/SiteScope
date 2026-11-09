import { useState } from 'react';

import { Card } from '../../components/Card';
import { Notice } from '../../components/feedback';
import type { CrawlPolicy } from '../../lib/api/types';
import { CreateJobForm } from './CreateJobForm';
import { useCrawlPolicy } from './hooks';
import { JobsTable } from './JobsTable';

export function CrawlJobsPage() {
  const [pageSize, setPageSize] = useState(10);
  const access = useCrawlPolicy().data?.write_access;
  const readOnly = access === 'token' || access === 'disabled';
  // The listing stays the second child in both layouts, so it never remounts.
  return (
    <div
      className={readOnly ? 'space-y-6' : 'grid gap-6 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]'}
    >
      {readOnly ? (
        <ReadOnlyNotice access={access} />
      ) : (
        <Card
          title="New crawl job"
          description="Breadth-first, limited to an approved target and this server's caps."
          className="self-start"
        >
          <CreateJobForm />
        </Card>
      )}
      <Card title="Crawl jobs" description="Newest first. Running jobs refresh by themselves.">
        <JobsTable pageSize={pageSize} onPageSizeChange={setPageSize} />
      </Card>
    </div>
  );
}

/** Shown instead of the form where the server refuses writes from this dashboard. Hiding the
 * form is a convenience only: the API enforces the write access (ADR-0024). */
function ReadOnlyNotice({ access }: { access: Exclude<CrawlPolicy['write_access'], 'open'> }) {
  return access === 'disabled' ? (
    <Notice title="This deployment is read-only">
      It accepts no new crawl jobs and no cancellations. The jobs below were crawled in advance:
      their pages, search, site analysis and link graph can all be explored.
    </Notice>
  ) : (
    <Notice title="Crawl jobs are managed by the operator">
      Creating or cancelling a crawl job needs the administrator&rsquo;s token, which this dashboard
      never holds. The jobs below, their search, site analysis and link graph can all be explored.
    </Notice>
  );
}
