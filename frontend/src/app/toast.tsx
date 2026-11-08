/**
 * Notifications for the results of actions (job created, cancellation requested). Shown in a
 * live region: successes politely, failures as alerts. They disappear on their own, and can be
 * closed; nothing important is shown only here (the page shows the resulting state too).
 */
import {
  CircleCheck,
  Info,
  OctagonExclamationPoint,
  TriangleExclamationPoint,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { cx } from '../components/cx';
import { ToastContext, type ToastInput, type ToastTone } from './toastContext';

interface Toast extends ToastInput {
  id: number;
}

const DURATION_MS: Record<ToastTone, number | null> = {
  success: 6000,
  info: 6000,
  warning: 9000,
  error: null,
};

const ICON: Record<ToastTone, ReactNode> = {
  success: <CircleCheck aria-hidden="true" className="size-4 text-good" />,
  info: <Info aria-hidden="true" className="size-4 text-accent-ink" />,
  warning: <TriangleExclamationPoint aria-hidden="true" className="size-4 text-warning" />,
  error: <OctagonExclamationPoint aria-hidden="true" className="size-4 text-critical-ink" />,
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback((toast: ToastInput) => {
    const id = nextId.current;
    nextId.current += 1;
    setToasts((current) => [...current.slice(-3), { ...toast, id }]);
  }, []);

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext value={value}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
        <div role="status" aria-live="polite" className="flex flex-col gap-2">
          {toasts
            .filter((toast) => toast.tone !== 'error')
            .map((toast) => (
              <ToastCard key={toast.id} toast={toast} onDismiss={dismiss} />
            ))}
        </div>
        <div role="alert" aria-live="assertive" className="flex flex-col gap-2">
          {toasts
            .filter((toast) => toast.tone === 'error')
            .map((toast) => (
              <ToastCard key={toast.id} toast={toast} onDismiss={dismiss} />
            ))}
        </div>
      </div>
    </ToastContext>
  );
}

function ToastCard({ toast, onDismiss }: { toast: Toast; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const duration = DURATION_MS[toast.tone];
    if (duration === null) return undefined; // errors stay until dismissed
    const timer = setTimeout(() => {
      onDismiss(toast.id);
    }, duration);
    return () => {
      clearTimeout(timer);
    };
  }, [toast.id, toast.tone, onDismiss]);

  return (
    <div
      className={cx(
        'pointer-events-auto flex items-start gap-3 rounded-lg border bg-surface px-4 py-3 shadow-xl',
        toast.tone === 'error' ? 'border-critical/50' : 'border-line-strong',
      )}
    >
      <span className="mt-0.5 shrink-0">{ICON[toast.tone]}</span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-ink">{toast.title}</p>
        {toast.message !== undefined && (
          <p className="mt-0.5 text-sm text-ink-2">{toast.message}</p>
        )}
      </div>
      <button
        type="button"
        onClick={() => {
          onDismiss(toast.id);
        }}
        aria-label="Dismiss notification"
        className="-mt-0.5 -mr-1 rounded p-1 text-ink-3 hover:bg-raised hover:text-ink"
      >
        <X aria-hidden="true" className="size-3.5" />
      </button>
    </div>
  );
}
