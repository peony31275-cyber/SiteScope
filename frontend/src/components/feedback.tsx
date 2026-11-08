/** Loading, empty, error and notice states, used the same way on every page. */
import {
  CircleCheck,
  Info,
  OctagonExclamationPoint,
  RotateCw,
  TriangleExclamationPoint,
} from 'lucide-react';
import type { ReactNode } from 'react';

import { describeError, type ErrorContext } from '../lib/api/errors';
import { Button } from './Button';
import { cx } from './cx';
import { Spinner } from './Spinner';

export function LoadingState({
  label = 'Loading…',
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <div
      role="status"
      className={cx('flex items-center justify-center gap-2 py-10 text-sm text-ink-3', className)}
    >
      <Spinner />
      {label}
    </div>
  );
}

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, children, action, className }: EmptyStateProps) {
  return (
    <div
      className={cx(
        'flex flex-col items-center justify-center gap-2 px-4 py-10 text-center',
        className,
      )}
    >
      {icon !== undefined && (
        <div
          aria-hidden="true"
          className="mb-1 rounded-full border border-line bg-raised p-2.5 text-ink-3"
        >
          {icon}
        </div>
      )}
      <p className="text-sm font-medium text-ink">{title}</p>
      {children !== undefined && <div className="max-w-md text-sm text-ink-3">{children}</div>}
      {action !== undefined && <div className="mt-2">{action}</div>}
    </div>
  );
}

interface ErrorStateProps {
  error: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  /** Replaces the title derived from the error. */
  title?: string;
  /** `action`: the failed request changes something (create, cancel); see `describeError`. */
  context?: ErrorContext;
  className?: string;
}

/** An API failure explained for people: what happened, what to do, and the request ID to
 * quote. Never a stack trace. */
export function ErrorState({
  error,
  onRetry,
  retrying = false,
  title,
  context = 'read',
  className,
}: ErrorStateProps) {
  const description = describeError(error, context);
  return (
    <div
      role="alert"
      className={cx('rounded-lg border border-critical/40 bg-critical/5 p-4', className)}
    >
      <div className="flex items-start gap-3">
        <OctagonExclamationPoint
          aria-hidden="true"
          className="mt-0.5 size-4 shrink-0 text-critical-ink"
        />
        <div className="min-w-0 flex-1 space-y-1.5">
          <p className="text-sm font-semibold text-ink">{title ?? description.title}</p>
          <p className="text-sm text-ink-2">{description.message}</p>
          {description.details.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink-2">
              {description.details.map((detail) => (
                <li key={detail}>{detail}</li>
              ))}
            </ul>
          )}
          {description.requestId !== null && (
            <p className="text-xs text-ink-3">
              Request ID <code className="font-mono text-ink-2">{description.requestId}</code>
            </p>
          )}
          {onRetry !== undefined && (
            <Button
              size="sm"
              className="mt-1"
              onClick={onRetry}
              busy={retrying}
              icon={<RotateCw aria-hidden="true" className="size-3.5" />}
            >
              Try again
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

type NoticeTone = 'info' | 'success' | 'warning' | 'danger';

const NOTICE: Record<NoticeTone, { box: string; icon: ReactNode }> = {
  info: {
    box: 'border-accent/35 bg-accent/5',
    icon: <Info aria-hidden="true" className="size-4 text-accent-ink" />,
  },
  success: {
    box: 'border-good/40 bg-good/5',
    icon: <CircleCheck aria-hidden="true" className="size-4 text-good" />,
  },
  warning: {
    box: 'border-warning/40 bg-warning/5',
    icon: <TriangleExclamationPoint aria-hidden="true" className="size-4 text-warning" />,
  },
  danger: {
    box: 'border-critical/40 bg-critical/5',
    icon: <OctagonExclamationPoint aria-hidden="true" className="size-4 text-critical-ink" />,
  },
};

interface NoticeProps {
  tone?: NoticeTone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  /** `status` announces politely when it appears; omit for static notes. */
  role?: 'status' | 'alert';
  className?: string;
}

/** A banner inside a page: a state the reader should know about (truncated graph, crawl still
 * running, cancellation settling). */
export function Notice({ tone = 'info', title, children, action, role, className }: NoticeProps) {
  return (
    <div
      role={role}
      className={cx(
        'flex items-start gap-3 rounded-lg border px-4 py-3',
        NOTICE[tone].box,
        className,
      )}
    >
      <span className="mt-0.5 shrink-0">{NOTICE[tone].icon}</span>
      <div className="min-w-0 flex-1 text-sm">
        {title !== undefined && <p className="font-medium text-ink">{title}</p>}
        {children !== undefined && (
          <div className={cx('text-ink-2', title !== undefined && 'mt-0.5')}>{children}</div>
        )}
      </div>
      {action !== undefined && <div className="shrink-0">{action}</div>}
    </div>
  );
}
