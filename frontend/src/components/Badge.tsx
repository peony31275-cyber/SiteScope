import type { ReactNode } from 'react';

import { cx } from './cx';
import { TONE_DOT, type Tone } from './tones';

const TONE_BADGE: Record<Tone, string> = {
  neutral: 'border-line-strong bg-raised text-ink-2',
  info: 'border-accent/40 bg-accent/10 text-ink',
  good: 'border-good/45 bg-good/10 text-ink',
  warning: 'border-warning/45 bg-warning/10 text-ink',
  serious: 'border-serious/45 bg-serious/10 text-ink',
  critical: 'border-critical/55 bg-critical/10 text-ink',
};

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  /** An animated dot: the state is changing right now (respects reduced motion). */
  live?: boolean;
  icon?: ReactNode;
  title?: string;
  className?: string;
}

export function Badge({
  tone = 'neutral',
  children,
  live = false,
  icon,
  title,
  className,
}: BadgeProps) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        TONE_BADGE[tone],
        className,
      )}
    >
      {icon ?? (
        <span
          aria-hidden="true"
          className={cx('size-1.5 rounded-full', TONE_DOT[tone], live && 'animate-pulse')}
        />
      )}
      {children}
    </span>
  );
}
