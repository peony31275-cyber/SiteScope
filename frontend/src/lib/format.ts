/** Display formatting: numbers, times, IDs and URLs. Pure functions; `now` is always passed in. */

const integer = new Intl.NumberFormat('en-US');
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const dateTime = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});
const relative = new Intl.RelativeTimeFormat('en-US', { numeric: 'auto' });

export function formatNumber(value: number): string {
  return integer.format(value);
}

/** 1,284 / 12.9K / 4.2M: compact from 10,000 on. */
export function formatCompact(value: number): string {
  return Math.abs(value) < 10_000 ? integer.format(value) : compact.format(value);
}

/** A share of a whole, or an em dash when the whole is empty. */
export function formatPercent(part: number, whole: number): string {
  if (whole <= 0) return '—';
  const share = (part / whole) * 100;
  if (share > 0 && share < 1) return '<1%';
  if (share < 100 && share > 99) return '>99%';
  return `${String(Math.round(share))}%`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : dateTime.format(date);
}

/** "12 s ago", "3 min ago", "2 h ago", "yesterday"; times in the future read as "just now"
 * (the server's and the browser's clocks differ slightly). */
export function formatRelative(iso: string | null | undefined, now: number): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const seconds = Math.round((then - now) / 1000);
  if (seconds > -5) return 'just now';
  const minutes = Math.round(seconds / 60);
  const hours = Math.round(seconds / 3600);
  const days = Math.round(seconds / 86_400);
  if (seconds > -60) return relative.format(seconds, 'second');
  if (minutes > -60) return relative.format(minutes, 'minute');
  if (hours > -24) return relative.format(hours, 'hour');
  return relative.format(days, 'day');
}

/** "4.2 s", "3 min 05 s", "2 h 07 min". */
export function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : String(Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return `${String(minutes)} min ${String(Math.floor(seconds % 60)).padStart(2, '0')} s`;
  return `${String(Math.floor(minutes / 60))} h ${String(minutes % 60).padStart(2, '0')} min`;
}

/** The last 8 characters of an ID, after an ellipsis. Job IDs are UUIDv7: their first block
 * is a timestamp, the same for jobs created within about a minute; the tail is random. */
export function shortId(id: string): string {
  return `…${id.slice(-8)}`;
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** The path, query and fragment of a URL ("/" for the root). */
export function pathOf(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return url;
  }
}
