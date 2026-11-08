import { Badge } from '../../components/Badge';
import type { IndexingState, JobStatus } from '../../lib/api/types';
import { INDEXING_STATUS, JOB_STATUS } from './status';

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const meaning = JOB_STATUS[status];
  return (
    <Badge
      tone={meaning.tone}
      live={status === 'running' || status === 'cancelling'}
      title={meaning.description}
    >
      {meaning.label}
    </Badge>
  );
}

export function IndexingBadge({ status }: { status: IndexingState }) {
  const meaning = INDEXING_STATUS[status];
  return (
    <Badge tone={meaning.tone} live={status === 'running'} title={meaning.description}>
      {meaning.label}
    </Badge>
  );
}
