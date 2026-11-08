import type { ReactNode } from 'react';

import { TONE_DOT, type Tone } from './tones';
import { cx } from './cx';

interface StatTileProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  /** A status dot beside the label (with the label, never alone). */
  tone?: Tone;
  className?: string;
}

/** A labeled number. Proportional figures: the value stands alone, it does not align in a
 * column. */
export function StatTile({ label, value, hint, tone, className }: StatTileProps) {
  return (
    <div className={cx('min-w-0 rounded-xl border border-line bg-surface px-4 py-3.5', className)}>
      <dt className="flex items-center gap-1.5 text-xs font-medium text-ink-3">
        {tone !== undefined && (
          <span aria-hidden="true" className={cx('size-1.5 rounded-full', TONE_DOT[tone])} />
        )}
        {label}
      </dt>
      <dd className="mt-1.5 truncate text-2xl font-semibold tracking-tight text-ink">{value}</dd>
      {hint !== undefined && <dd className="mt-1 text-xs text-ink-3">{hint}</dd>}
    </div>
  );
}

/** A grid of stat tiles: a definition list, so each value is announced with its label. */
export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <dl className={cx('grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4', className)}>
      {children}
    </dl>
  );
}
