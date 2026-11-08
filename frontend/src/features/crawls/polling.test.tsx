/**
 * Polling of running jobs: the cadence rules (pure), and the real hooks on fake timers with
 * TanStack Query: active jobs refresh at the interval, settled jobs stop, a job that shows no
 * change pauses, hidden tabs and unmounted pages stop.
 */
import { focusManager } from '@tanstack/react-query';
import { act, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FakeApi, json } from '../../tests/fakeApi';
import { detail, JOB_ID, settled, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';
import { jobRefetchInterval, newer, progressSignature, stallWindowMs } from './status';

describe('jobRefetchInterval', () => {
  it('refreshes running crawls at the base interval', () => {
    expect(jobRefetchInterval(detail({ status: 'running' }), 3000)).toBe(3000);
    expect(jobRefetchInterval(detail({ status: 'queued' }), 3000)).toBe(3000);
    expect(jobRefetchInterval(detail({ status: 'cancelling' }), 2000)).toBe(2000);
  });

  it('refreshes more slowly once the crawl has ended but indexing has not', () => {
    expect(jobRefetchInterval(detail({ status: 'completed' }), 2000)).toBe(5000);
    expect(jobRefetchInterval(detail({ status: 'cancelled' }), 10_000)).toBe(20_000);
  });

  it('never refreshes a settled job, or when auto-refresh is off', () => {
    expect(jobRefetchInterval(settled(), 2000)).toBe(false);
    expect(jobRefetchInterval(detail(), null)).toBe(false);
    expect(jobRefetchInterval(undefined, 2000)).toBe(false);
  });

  it('slows secondary views by a factor', () => {
    expect(jobRefetchInterval(detail(), 3000, 2)).toBe(6000);
  });

  it('gives waiting crawls longer than waiting indexing before pausing', () => {
    expect(stallWindowMs(detail())).toBeGreaterThan(stallWindowMs(detail({ status: 'completed' })));
  });
});

describe('progressSignature and newer', () => {
  it('changes with any progress, not with nothing', () => {
    const job = detail();
    expect(progressSignature(job)).toBe(progressSignature(detail()));
    const progressed = detail({ progress: { ...job.progress, pages_stored: 4 } });
    expect(progressSignature(progressed)).not.toBe(progressSignature(job));
  });

  it('prefers the more recently updated view of a job', () => {
    const older = summary({ status: 'running', updated_at: '2026-11-07T10:00:05Z' });
    const later = summary({ status: 'completed', updated_at: '2026-11-07T10:00:09Z' });
    expect(newer(older, later).status).toBe('completed');
    expect(newer(later, older).status).toBe('completed');
  });
});

describe('Polling a job (fake timers)', () => {
  afterEach(() => {
    focusManager.setFocused(undefined);
  });

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it('refreshes a running job at the interval and stops once it is settled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let job = detail();
    const api = new FakeApi().on('GET', '/crawls/:jobId', () => json(job)).install();
    renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 1000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });
    const initial = api.calls(`/crawls/${JOB_ID}`).length;

    await advance(3100);
    expect(api.calls(`/crawls/${JOB_ID}`).length - initial).toBeGreaterThanOrEqual(3);

    job = settled();
    await advance(1100);
    await screen.findByText(/Final state/);
    const afterSettling = api.calls(`/crawls/${JOB_ID}`).length;
    await advance(30_000);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBe(afterSettling);
  });

  it('does not refresh while the tab is hidden', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = new FakeApi().on('GET', '/crawls/:jobId', () => json(detail())).install();
    renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 1000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });
    focusManager.setFocused(false);
    const before = api.calls(`/crawls/${JOB_ID}`).length;
    await advance(5000);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBe(before);
  });

  it('stops polling when the page is left', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = new FakeApi().on('GET', '/crawls/:jobId', () => json(detail())).install();
    const { unmount } = renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 1000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });
    unmount();
    const before = api.calls(`/crawls/${JOB_ID}`).length;
    await advance(5000);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBe(before);
  });

  it('pauses a job that shows no change, and resumes on request', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = new FakeApi()
      .on('GET', '/crawls/:jobId', () => json(detail({ status: 'queued' })))
      .install();
    const { user } = renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 10_000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });

    await advance(5 * 60_000 + 1000);
    expect(
      await screen.findByText('Auto-refresh paused: no change for several minutes'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Check that the scheduler and the crawler workers are running/),
    ).toBeInTheDocument();
    const paused = api.calls(`/crawls/${JOB_ID}`).length;
    await advance(60_000);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBe(paused);

    await user.click(screen.getByRole('button', { name: 'Resume' }));
    await advance(10_100);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBeGreaterThan(paused);
  });

  it('never pauses a job because its tab was hidden for a while (review finding)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let reads = 0;
    const api = new FakeApi()
      .on('GET', '/crawls/:jobId', () => {
        reads += 1;
        return json(detail({ progress: { ...detail().progress, urls_finished: reads } }));
      })
      .install();
    renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 10_000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });
    focusManager.setFocused(false);
    await advance(10 * 60_000); // ten minutes in another tab: nothing is read
    focusManager.setFocused(true);
    await advance(10_100);
    expect(screen.queryByText(/Auto-refresh paused/)).not.toBeInTheDocument();
    const before = api.calls(`/crawls/${JOB_ID}`).length;
    await advance(20_100);
    expect(api.calls(`/crawls/${JOB_ID}`).length).toBeGreaterThan(before);
  });

  it('keeps the last state on screen when a refresh fails, and says so', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let online = true;
    new FakeApi()
      .on('GET', '/crawls/:jobId', () => {
        if (!online) throw new TypeError('Failed to fetch');
        return json(detail());
      })
      .install();
    renderApp(`/crawls/${JOB_ID}`, {
      pollIntervalMs: 1000,
      advanceTimers: vi.advanceTimersByTimeAsync,
    });
    await screen.findByRole('region', { name: 'Crawl job' });
    online = false;
    await advance(1100);
    expect(
      await screen.findByText('Refresh failed: Cannot reach the SiteScope API'),
    ).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Crawl job' })).toHaveTextContent(JOB_ID);
  });
});
