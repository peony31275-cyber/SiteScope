import { describe, expect, it } from 'vitest';

import {
  parseDepth,
  parseIndexStatus,
  parseLinkClass,
  parseSourcePage,
  parseTriState,
} from './filters';

describe('listing filters from the address', () => {
  it('accept only the API’s own values', () => {
    expect(parseLinkClass('robots_blocked')).toBe('robots_blocked');
    expect(parseIndexStatus('noindex')).toBe('noindex');
    expect(parseTriState('false')).toBe('false');
    expect(parseDepth('3')).toBe(3);
    expect(parseSourcePage('0199A1B2-8D4E-7F50-9B0C-1D2E3F4A5B6C')).toBe(
      '0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c',
    );
  });

  it('turn anything else into no filter, never into a request (review finding)', () => {
    for (const key of ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'BROKEN', '']) {
      expect(parseLinkClass(key)).toBeNull();
    }
    expect(parseLinkClass(null)).toBeNull();
    expect(parseIndexStatus('constructor')).toBeNull();
    expect(parseTriState('yes')).toBe('any');
    expect(parseDepth('1001')).toBeNull();
    expect(parseDepth('-1')).toBeNull();
    expect(parseSourcePage('../x')).toBeNull();
  });
});
