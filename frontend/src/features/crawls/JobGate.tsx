import { MousePointerClick } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router';

import { EmptyState } from '../../components/feedback';
import { useSelectedJob } from '../../hooks/useSelectedJob';

/** Renders a job page once a valid crawl job is selected (`?job=`); otherwise asks for one. */
export function JobGate({ children }: { children: (jobId: string) => ReactNode }) {
  const { jobId, invalid } = useSelectedJob();
  if (invalid) {
    return (
      <EmptyState
        icon={<MousePointerClick className="size-5" />}
        title="The address names no valid crawl job"
      >
        The <code className="font-mono">job</code> parameter is not a job ID. Choose a job with the
        selector above.
      </EmptyState>
    );
  }
  if (jobId === null) {
    return (
      <EmptyState icon={<MousePointerClick className="size-5" />} title="Select a crawl job">
        Choose a job with the selector above, or start one on the{' '}
        <Link to="/crawls" className="text-accent-ink hover:underline">
          Crawl Jobs
        </Link>{' '}
        page.
      </EmptyState>
    );
  }
  return children(jobId);
}
