import { useQuery } from '@tanstack/react-query';

import { Select } from '../../components/Field';
import { getCrawl } from '../../lib/api/endpoints';
import { queryKeys } from '../../lib/api/queryKeys';
import { shortId } from '../../lib/format';
import { useSelectedJob } from '../../hooks/useSelectedJob';
import { useCrawlList } from './hooks';
import { jobOptionLabel } from './status';

/** How many recent jobs the selector offers: one page of the listing, never every job. */
const SELECTOR_JOBS = 50;

/** The header's job picker, for the pages that work on one job (search, analysis, graph). */
export function JobSelector() {
  const { jobId, select } = useSelectedJob();
  const list = useCrawlList(SELECTOR_JOBS, null);
  const listed = list.data?.items ?? [];
  const missing = jobId !== null && list.isSuccess && !listed.some((job) => job.id === jobId);
  // A job older than the most recent ones (a shared link): read it on its own.
  const selected = useQuery({
    queryKey: queryKeys.crawl(jobId ?? ''),
    queryFn: ({ signal }) => getCrawl(jobId ?? '', signal),
    enabled: missing,
  });

  return (
    <div className="flex min-w-0 items-center gap-2">
      <label
        htmlFor="job-selector"
        className="hidden text-xs font-medium whitespace-nowrap text-ink-3 sm:block"
      >
        Crawl job
      </label>
      <Select
        id="job-selector"
        aria-label="Crawl job"
        value={jobId ?? ''}
        onChange={(event) => {
          select(event.target.value === '' ? null : event.target.value);
        }}
        className="w-full font-mono text-xs sm:w-96"
      >
        <option value="">{list.isPending ? 'Loading jobs…' : 'Select a crawl job…'}</option>
        {missing && (
          <option value={jobId}>
            {selected.data ? jobOptionLabel(selected.data) : `${shortId(jobId)} · …`}
          </option>
        )}
        {listed.map((job) => (
          <option key={job.id} value={job.id}>
            {jobOptionLabel(job)}
          </option>
        ))}
      </Select>
    </div>
  );
}
