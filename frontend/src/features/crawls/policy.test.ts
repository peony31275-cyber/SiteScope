import { describe, expect, it } from 'vitest';

import { policy } from '../../tests/fixtures';
import { limitProblem, newIdempotencyKey, seedUrlProblem, suggestedSeeds } from './policy';

describe('seedUrlProblem', () => {
  const rules = policy({
    approved_hosts: ['docs.example.com', '*.example.org', '127.0.0.1', '[::1]'],
    allowed_ports: [80, 443, 8001],
  });

  it.each([
    ['https://docs.example.com/guide', null],
    ['http://127.0.0.1:8001/', null],
    ['http://[::1]:8001/', null],
    ['https://www.example.org/', null],
    ['https://deep.sub.example.org/', null],
  ])('accepts %s', (url, expected) => {
    expect(seedUrlProblem(url, rules)).toBe(expected);
  });

  it.each([
    ['', 'Enter the URL the crawl starts from.'],
    ['docs.example.com', 'Enter an absolute URL, such as https://docs.example.com/.'],
    [
      'ftp://docs.example.com/',
      'The scheme "ftp" is not allowed on this server (allowed: http, https).',
    ],
    ['https://user:pw@docs.example.com/', 'URLs with a user name or password are not accepted.'],
    ['https://example.org/', '"example.org" is not an approved crawl target on this server.'],
    [
      'https://evil-example.org/',
      '"evil-example.org" is not an approved crawl target on this server.',
    ],
    [
      'https://docs.example.com.evil.test/',
      '"docs.example.com.evil.test" is not an approved crawl target on this server.',
    ],
    [
      'https://docs.example.com:8443/',
      'Port 8443 is not allowed on this server (allowed: 80, 443, 8001).',
    ],
  ])('refuses %s', (url, message) => {
    expect(seedUrlProblem(url, rules)).toBe(message);
  });

  it('refuses a URL longer than the API accepts', () => {
    expect(seedUrlProblem(`https://docs.example.com/${'a'.repeat(4096)}`, rules)).toMatch(
      /longer than 4096/,
    );
  });
});

describe('limitProblem', () => {
  const depth = { min: 0, max: 5, name: 'maximum depth' };

  it('accepts whole numbers in range only, without changing them', () => {
    expect(limitProblem('0', depth)).toBeNull();
    expect(limitProblem('5', depth)).toBeNull();
    expect(limitProblem('6', depth)).toBe(
      'The maximum depth must be between 0 and 5 on this server.',
    );
    expect(limitProblem('-1', depth)).toBe('The maximum depth must be a whole number.');
    expect(limitProblem('1e2', depth)).toBe('The maximum depth must be a whole number.');
    expect(limitProblem(' ', depth)).toBe('Enter the maximum depth.');
  });
});

describe('suggestedSeeds', () => {
  it('offers the root of each exact approved host, with sensible scheme and port pairs', () => {
    expect(
      suggestedSeeds(
        policy({ approved_hosts: ['127.0.0.1', '*.example.org'], allowed_ports: [80, 443, 8001] }),
      ),
    ).toEqual([
      'http://127.0.0.1/',
      'http://127.0.0.1:8001/',
      'https://127.0.0.1/',
      'https://127.0.0.1:8001/',
    ]);
  });
});

describe('IPv6 targets (review finding)', () => {
  it('match and are suggested in the server’s bracketed form', () => {
    const rules = policy({
      approved_hosts: ['[::1]'],
      allowed_schemes: ['http'],
      allowed_ports: [8001],
    });
    expect(suggestedSeeds(rules)).toEqual(['http://[::1]:8001/']);
    expect(seedUrlProblem('http://[::1]:8001/', rules)).toBeNull();
    expect(seedUrlProblem('http://[::2]:8001/', rules)).toBe(
      '"[::2]" is not an approved crawl target on this server.',
    );
  });
});

describe('newIdempotencyKey', () => {
  it('is a visible-ASCII key the API accepts, new for every submission', () => {
    const key = newIdempotencyKey();
    expect(key).toMatch(/^[\x21-\x7e]{1,255}$/);
    expect(newIdempotencyKey()).not.toBe(key);
  });
});
