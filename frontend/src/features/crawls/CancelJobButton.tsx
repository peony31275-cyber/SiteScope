import { Ban } from 'lucide-react';
import { useState } from 'react';

import { useToast } from '../../app/toastContext';
import { Button } from '../../components/Button';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { describeError } from '../../lib/api/errors';
import type { CrawlJobDetail } from '../../lib/api/types';
import { hostOf, shortId } from '../../lib/format';
import { useCancelCrawl } from './hooks';
import { isCancellable, JOB_STATUS } from './status';

interface CancelJobButtonProps {
  job: CrawlJobDetail;
  /** Called once the server confirmed the cancellation (this button then disappears). */
  onCancelled?: () => void;
}

/**
 * Cancel a crawl, after confirmation. What is shown afterwards is what the server answered,
 * never a guess: 200 `cancelled` (done), 202 `cancelling` (URLs in flight still finishing), or
 * 200 with a job that had already ended (the cancellation came too late, nothing changed).
 * The page renders one of these per job (`JobDetail` is keyed by the job's ID), so an open
 * dialog can never act on another job.
 */
export function CancelJobButton({ job, onCancelled }: CancelJobButtonProps) {
  const [open, setOpen] = useState(false);
  const cancel = useCancelCrawl(job.id);
  const { notify } = useToast();

  if (!isCancellable(job.status)) return null;

  function confirm() {
    cancel.mutate(undefined, {
      onSuccess: ({ job: result, settling }) => {
        setOpen(false);
        onCancelled?.();
        if (settling) {
          notify({
            tone: 'info',
            title: 'Cancellation requested',
            message:
              'Nothing new is dispatched; the URLs in flight are finishing, then the job is cancelled.',
          });
        } else if (result.status === 'cancelled') {
          notify({
            tone: 'success',
            title: 'Crawl cancelled',
            message: 'Pages already stored are kept and stay searchable.',
          });
        } else {
          notify({
            tone: 'warning',
            title: `The crawl had already ${JOB_STATUS[result.status].label.toLowerCase()}`,
            message: 'It ended before the cancellation reached the server; nothing was changed.',
          });
        }
      },
      onError: (error) => {
        setOpen(false);
        const description = describeError(error, 'action');
        notify({
          tone: 'error',
          title: `Cancellation failed: ${description.title}`,
          message: description.message,
        });
      },
    });
  }

  return (
    <>
      <Button
        variant="danger"
        size="sm"
        onClick={() => {
          setOpen(true);
        }}
        icon={<Ban aria-hidden="true" className="size-3.5" />}
      >
        Cancel crawl
      </Button>
      <ConfirmDialog
        open={open}
        title={`Cancel crawl job ${shortId(job.id)} of ${hostOf(job.seed_url)}?`}
        confirmLabel="Cancel the crawl"
        cancelLabel="Keep crawling"
        confirmVariant="danger"
        busy={cancel.isPending}
        onConfirm={confirm}
        onCancel={() => {
          setOpen(false);
        }}
      >
        <p>
          Job <span className="font-mono text-xs break-all text-ink">{job.id}</span>, seed{' '}
          <span className="font-mono text-xs break-all text-ink">{job.seed_url}</span>.
        </p>
        <p>
          The crawl stops: nothing new is dispatched or claimed, and URLs waiting to be crawled are
          cancelled at once. URLs being fetched right now finish first, usually within seconds.
        </p>
        <p>
          Pages already stored are kept, and they are still indexed and searchable. This cannot be
          undone.
        </p>
      </ConfirmDialog>
    </>
  );
}
