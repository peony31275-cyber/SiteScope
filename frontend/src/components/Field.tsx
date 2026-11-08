import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react';

import { cx } from './cx';

const CONTROL =
  'h-9 min-w-0 rounded-md border border-line-strong bg-raised px-3 text-sm text-ink ' +
  'placeholder:text-ink-3 transition-colors hover:border-ink-3/60 focus-visible:border-accent ' +
  'aria-[invalid=true]:border-critical disabled:cursor-not-allowed disabled:opacity-60';

/** Full width unless the caller sets a width: two width utilities on one element would be
 * decided by stylesheet order, not by the order of the classes. */
function controlClasses(className: string | undefined): string {
  const sized = className !== undefined && /(^|\s)([a-z0-9]+:)*w-/.test(className);
  return cx(CONTROL, !sized && 'w-full', className);
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={controlClasses(className)} {...rest} />;
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cx(controlClasses(className), 'pr-8')} {...rest}>
      {children}
    </select>
  );
}

interface FieldProps {
  /** The control's `id`; the label, hint and error are wired to it. */
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  className?: string;
}

/** A label, a control, and its hint or error. The control must set
 * `aria-describedby={describedBy(id, hint, error)}` and `aria-invalid` itself. */
export function Field({ id, label, hint, error, children, className }: FieldProps) {
  return (
    <div className={cx('min-w-0 space-y-1.5', className)}>
      <label htmlFor={id} className="block text-xs font-medium text-ink-2">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-critical-ink">
          {error}
        </p>
      ) : (
        hint !== undefined && (
          <p id={`${id}-hint`} className="text-xs text-ink-3">
            {hint}
          </p>
        )
      )}
    </div>
  );
}
