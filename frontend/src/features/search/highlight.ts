/**
 * Search highlights. The API gives `[start, end)` ranges of Unicode code points in the snippet
 * (docs/api-conventions.md, "Search"). JavaScript strings are indexed by UTF-16 code units, so
 * an emoji or another character beyond the Basic Multilingual Plane counts once for the server
 * and twice for `String.prototype.slice`. The snippet is therefore split into code points with
 * `Array.from` first, and the ranges index that array.
 *
 * Ranges are untrusted input: they are clamped to the snippet, empty ones are dropped, and
 * overlapping or touching ones are merged, so the text is always rendered exactly once.
 */
import type { HighlightRange } from '../../lib/api/types';

export interface TextSegment {
  text: string;
  highlighted: boolean;
}

export function highlightSegments(
  snippet: string,
  ranges: readonly HighlightRange[],
): TextSegment[] {
  const points = Array.from(snippet);
  const length = points.length;
  const clamped = ranges
    .filter((range) => Number.isInteger(range.start) && Number.isInteger(range.end))
    .map((range) => ({
      start: Math.min(length, Math.max(0, range.start)),
      end: Math.min(length, Math.max(0, range.end)),
    }))
    .filter((range) => range.end > range.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const merged: { start: number; end: number }[] = [];
  for (const range of clamped) {
    const last = merged[merged.length - 1];
    if (last !== undefined && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ ...range });
  }

  const segments: TextSegment[] = [];
  let position = 0;
  for (const { start, end } of merged) {
    if (start > position)
      segments.push({ text: points.slice(position, start).join(''), highlighted: false });
    segments.push({ text: points.slice(start, end).join(''), highlighted: true });
    position = end;
  }
  if (position < length)
    segments.push({ text: points.slice(position).join(''), highlighted: false });
  return segments;
}
