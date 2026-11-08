import { useState } from 'react';

import { Card } from '../../components/Card';
import { CreateJobForm } from './CreateJobForm';
import { JobsTable } from './JobsTable';

export function CrawlJobsPage() {
  const [pageSize, setPageSize] = useState(10);
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
      <Card
        title="New crawl job"
        description="Breadth-first, limited to an approved target and this server's caps."
        className="self-start"
      >
        <CreateJobForm />
      </Card>
      <Card title="Crawl jobs" description="Newest first. Running jobs refresh by themselves.">
        <JobsTable pageSize={pageSize} onPageSizeChange={setPageSize} />
      </Card>
    </div>
  );
}
