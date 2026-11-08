import { describe, expect, it } from 'vitest';

import { pageNumbers } from '../components/a11y';
import {
  formatCompact,
  formatDuration,
  formatPercent,
  formatRelative,
  hostOf,
  pathOf,
  shortId,
} from './format';
import { isHttpUrl, isUuid } from './urls';

describe('formatting', () => {
  it('formats counts, shares and durations', () => {
    expect(formatCompact(1284)).toBe('1,284');
    expect(formatCompact(12_900)).toBe('12.9K');
    expect(formatPercent(31, 34)).toBe('91%');
    expect(formatPercent(1, 1000)).toBe('<1%');
    expect(formatPercent(999, 1000)).toBe('>99%');
    expect(formatPercent(0, 0)).toBe('—');
    expect(formatDuration(4200)).toBe('4.2 s');
    expect(formatDuration(185_000)).toBe('3 min 05 s');
    expect(formatDuration(7_620_000)).toBe('2 h 07 min');
  });

  it('formats times relative to a given now, and tolerates clock skew', () => {
    const now = Date.parse('2026-11-07T10:10:00Z');
    expect(formatRelative('2026-11-07T10:09:30Z', now)).toBe('30 seconds ago');
    expect(formatRelative('2026-11-07T10:07:00Z', now)).toBe('3 minutes ago');
    expect(formatRelative('2026-11-07T10:10:02Z', now)).toBe('just now');
    expect(formatRelative(null, now)).toBe('—');
  });

  it('shortens IDs and splits URLs', () => {
    // UUIDv7: the first block is a timestamp; the tail tells jobs apart.
    expect(shortId('0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b')).toBe('…2e3f4a5b');
    expect(shortId('0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a6c')).not.toBe(
      shortId('0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b'),
    );
    expect(hostOf('http://127.0.0.1:8001/a')).toBe('127.0.0.1:8001');
    expect(pathOf('http://127.0.0.1:8001/a/b?x=1')).toBe('/a/b?x=1');
    expect(pathOf('not a url')).toBe('not a url');
  });
});

describe('untrusted values', () => {
  it('links only http(s) URLs', () => {
    expect(isHttpUrl('https://docs.example.com/')).toBe(true);
    expect(isHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isHttpUrl('data:text/html,<b>x</b>')).toBe(false);
    expect(isHttpUrl('//evil.test/')).toBe(false);
  });

  it('recognizes job IDs', () => {
    expect(isUuid('0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b')).toBe(true);
    expect(isUuid('0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b/../../x')).toBe(false);
  });
});

describe('pageNumbers', () => {
  it('shows the ends and the neighbors of the current page, with gaps', () => {
    expect(pageNumbers(1, 1)).toEqual([1]);
    expect(pageNumbers(6, 20)).toEqual([1, null, 4, 5, 6, 7, 8, null, 20]);
    expect(pageNumbers(2, 5)).toEqual([1, 2, 3, 4, 5]);
  });
});
