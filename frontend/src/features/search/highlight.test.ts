import { describe, expect, it } from 'vitest';

import { highlightSegments } from './highlight';

/** The highlighted text of each range, as a reader sees it. */
function marked(snippet: string, ranges: { start: number; end: number }[]): string[] {
  return highlightSegments(snippet, ranges)
    .filter((segment) => segment.highlighted)
    .map((segment) => segment.text);
}

function joined(snippet: string, ranges: { start: number; end: number }[]): string {
  return highlightSegments(snippet, ranges)
    .map((segment) => segment.text)
    .join('');
}

describe('highlightSegments', () => {
  it('marks ASCII words', () => {
    expect(
      marked('Consumer groups let several workers share one stream', [
        { start: 0, end: 8 },
        { start: 9, end: 15 },
      ]),
    ).toEqual(['Consumer', 'groups']);
  });

  it('counts Chinese characters as one code point each', () => {
    const snippet = '分布式爬虫与搜索';
    expect(marked(snippet, [{ start: 3, end: 5 }])).toEqual(['爬虫']);
  });

  it('counts an emoji as one code point, not two UTF-16 units', () => {
    const snippet = '😀 crawl politely';
    // Code points: 😀=0, space=1, "crawl" = 2..7. String.slice would cut "cr" in half instead.
    expect(marked(snippet, [{ start: 2, end: 7 }])).toEqual(['crawl']);
    expect(snippet.slice(2, 7)).not.toBe('crawl');
  });

  it('handles supplementary-plane letters and CJK extension characters', () => {
    const snippet = '𝒳 marks 𠀀 index';
    // 𝒳 (U+1D4B3) and 𠀀 (U+20000) are one code point each: "marks" is 2..7, "index" 10..15.
    expect(
      marked(snippet, [
        { start: 2, end: 7 },
        { start: 10, end: 15 },
      ]),
    ).toEqual(['marks', 'index']);
    expect(marked(snippet, [{ start: 8, end: 9 }])).toEqual(['𠀀']);
  });

  it('marks several ranges and keeps the text between them', () => {
    const segments = highlightSegments('robots and politeness and leases', [
      { start: 0, end: 6 },
      { start: 11, end: 21 },
      { start: 26, end: 32 },
    ]);
    expect(segments).toEqual([
      { text: 'robots', highlighted: true },
      { text: ' and ', highlighted: false },
      { text: 'politeness', highlighted: true },
      { text: ' and ', highlighted: false },
      { text: 'leases', highlighted: true },
    ]);
  });

  it('merges overlapping and touching ranges, in any order', () => {
    expect(
      marked('abcdefgh', [
        { start: 4, end: 6 },
        { start: 0, end: 3 },
        { start: 2, end: 4 },
      ]),
    ).toEqual(['abcdef']);
  });

  it('clamps out-of-range offsets and drops empty or inverted ranges', () => {
    expect(marked('short', [{ start: 2, end: 99 }])).toEqual(['ort']);
    expect(
      marked('short', [
        { start: 3, end: 3 },
        { start: 4, end: 1 },
        { start: 50, end: 60 },
      ]),
    ).toEqual([]);
  });

  it('renders every character exactly once', () => {
    const snippet = '中文 😀 text 𝒳';
    expect(
      joined(snippet, [
        { start: 1, end: 4 },
        { start: 3, end: 9 },
        { start: 100, end: 120 },
      ]),
    ).toBe(snippet);
    expect(joined('', [{ start: 0, end: 4 }])).toBe('');
  });
});
