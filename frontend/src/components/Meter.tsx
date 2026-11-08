import { formatNumber, formatPercent } from '../lib/format';
import { cx } from './cx';

interface MeterProps {
  value: number;
  max: number;
  /** The accessible name, such as "URLs finished". */
  label: string;
  /** Fill color class; blue by default. */
  fillClassName?: string;
  className?: string;
}

/** A progress bar. The numbers are always printed next to it by the caller; the bar repeats
 * them as a proportion. */
export function Meter({ value, max, label, fillClassName = 'bg-accent', className }: MeterProps) {
  const share = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={Math.min(value, max)}
      aria-valuetext={`${formatNumber(value)} of ${formatNumber(max)} (${formatPercent(value, max)})`}
      className={cx('h-1.5 w-full overflow-hidden rounded-full bg-raised', className)}
    >
      <div
        className={cx('h-full rounded-full transition-[width] duration-500', fillClassName)}
        style={{ width: `${String(share * 100)}%` }}
      />
    </div>
  );
}

export interface Segment {
  key: string;
  label: string;
  value: number;
  /** Fill color class. */
  colorClassName: string;
}

interface SegmentedBarProps {
  segments: readonly Segment[];
  /** The accessible name of the whole bar, such as "Search documents by status". */
  label: string;
  className?: string;
}

/**
 * One whole split into parts (a thin segmented bar), with its legend: every part's name,
 * count and share in text, so the bar never carries a number alone. Parts at zero draw
 * nothing; a 2px surface gap separates the others.
 */
export function SegmentedBar({ segments, label, className }: SegmentedBarProps) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  const summary = segments
    .map((segment) => `${formatNumber(segment.value)} ${segment.label}`)
    .join(', ');
  return (
    <div className={className}>
      <div
        role="img"
        aria-label={`${label}: ${total === 0 ? 'none' : summary}`}
        className="flex h-2.5 w-full gap-0.5 overflow-hidden rounded-full bg-raised"
      >
        {total > 0 &&
          segments
            .filter((segment) => segment.value > 0)
            .map((segment) => (
              <div
                key={segment.key}
                title={`${segment.label}: ${formatNumber(segment.value)} (${formatPercent(segment.value, total)})`}
                className={cx(
                  'h-full first:rounded-l-full last:rounded-r-full',
                  segment.colorClassName,
                )}
                style={{ width: `${String((segment.value / total) * 100)}%` }}
              />
            ))}
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-xs">
        {segments.map((segment) => (
          <li key={segment.key} className="flex items-center gap-1.5 text-ink-2">
            <span aria-hidden="true" className={cx('size-2 rounded-sm', segment.colorClassName)} />
            {segment.label}
            <span className="font-medium text-ink tabular-nums">{formatNumber(segment.value)}</span>
            <span className="text-ink-3 tabular-nums">{formatPercent(segment.value, total)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
