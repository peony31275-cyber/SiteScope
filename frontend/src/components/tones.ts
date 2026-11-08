/** Meaning, not decoration: status colors are reserved for state, and a status color always
 * comes with a text label, so color is never the only signal. */
export type Tone = 'neutral' | 'info' | 'good' | 'warning' | 'serious' | 'critical';

export const TONE_DOT: Record<Tone, string> = {
  neutral: 'bg-neutral',
  info: 'bg-accent',
  good: 'bg-good',
  warning: 'bg-warning',
  serious: 'bg-serious',
  critical: 'bg-critical',
};
