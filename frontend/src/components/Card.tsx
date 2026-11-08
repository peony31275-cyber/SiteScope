import { useId, type ReactNode } from 'react';

import { cx } from './cx';

interface CardProps {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Keep the previous content visible but dimmed while it refreshes (no layout jump). */
  dimmed?: boolean;
}

/** A panel: an optional header (title, description, actions) over its content. */
export function Card({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
  dimmed,
}: CardProps) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={title === undefined ? undefined : headingId}
      className={cx('min-w-0 rounded-xl border border-line bg-surface', className)}
    >
      {title !== undefined && (
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-line px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <h2 id={headingId} className="text-sm font-semibold text-ink">
              {title}
            </h2>
            {description !== undefined && (
              <p className="mt-0.5 text-xs text-ink-3">{description}</p>
            )}
          </div>
          {actions !== undefined && (
            <div className="flex flex-wrap items-center gap-2">{actions}</div>
          )}
        </header>
      )}
      <div className={cx('p-4 sm:p-5', dimmed && 'opacity-60 transition-opacity', bodyClassName)}>
        {children}
      </div>
    </section>
  );
}
