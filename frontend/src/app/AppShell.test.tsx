import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeApi, json, problem } from '../tests/fakeApi';
import { JOB_ID, overview, settled, stats, summary } from '../tests/fixtures';
import { renderApp } from '../tests/render';

function shellApi() {
  return new FakeApi()
    .on('GET', '/overview', () => json(overview()))
    .on('GET', '/crawls', () => json({ items: [summary()], next_cursor: null }))
    .on('GET', '/crawls/:jobId', () => json(settled()))
    .on('GET', '/crawls/:jobId/stats', () => json(stats()))
    .on('GET', '/crawl-policy', () =>
      json({
        approved_hosts: [],
        allowed_schemes: ['http'],
        allowed_ports: [80],
        max_depth_limit: 5,
        max_pages_limit: 1000,
        max_active_jobs: 50,
        write_access: 'open',
      }),
    );
}

describe('Application shell', () => {
  it('navigates with the sidebar, and job pages keep the selected job', async () => {
    shellApi().install();
    const { user, router } = renderApp(`/search?job=${JOB_ID}`);
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Search explorer' }),
    ).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Main' });

    await user.click(within(nav).getByRole('link', { name: 'Site Analysis' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Site analysis' }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe(`?job=${JOB_ID}`);

    await user.click(within(nav).getByRole('link', { name: 'Crawl Jobs' }));
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Crawl jobs' }),
    ).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Crawl Jobs' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(document.title).toBe('Crawl jobs · SiteScope');
  });

  it('carries the job of a job detail page into the job views of the sidebar', async () => {
    shellApi().install();
    renderApp(`/crawls/${JOB_ID}`);
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    expect(within(nav).getByRole('link', { name: 'Search Explorer' })).toHaveAttribute(
      'href',
      `/search?job=${JOB_ID}`,
    );
    expect(within(nav).getByRole('link', { name: 'Crawl Jobs' })).toHaveAttribute(
      'href',
      '/crawls',
    );
  });

  it('changes the job of a job page with the header selector', async () => {
    const other = '0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a6c';
    shellApi()
      .on('GET', '/crawls', () =>
        json({
          items: [summary(), summary({ id: other, seed_url: 'http://127.0.0.1:8002/' })],
          next_cursor: null,
        }),
      )
      .install();
    const { user, router } = renderApp(`/analysis?job=${JOB_ID}&tab=links&class=broken`);
    const selector = await screen.findByLabelText('Crawl job');
    await waitFor(() => {
      expect(within(selector).getAllByRole('option')).toHaveLength(3);
    });
    await user.selectOptions(selector, other);
    const params = new URLSearchParams(router.state.location.search);
    expect(params.get('job')).toBe(other);
    expect(params.get('class')).toBeNull(); // per-job filters are dropped
    expect(params.get('tab')).toBe('links');
  });

  it('clears the job selection, and keeps it cleared (review finding)', async () => {
    shellApi().install();
    const { user, router } = renderApp(`/search?job=${JOB_ID}`);
    const selector = await screen.findByLabelText('Crawl job');
    await waitFor(() => {
      expect(selector).toHaveValue(JOB_ID);
    });
    await user.selectOptions(selector, '');
    expect(await screen.findByText('Select a crawl job')).toBeInTheDocument();
    expect(new URLSearchParams(router.state.location.search).get('job')).toBeNull();
    expect(window.localStorage.getItem('sitescope.lastJobId')).toBeNull();
  });

  it('shows the auto-refresh interval in use, also one that is not offered (review finding)', async () => {
    shellApi().install();
    renderApp('/', { pollIntervalMs: 15_000 });
    const select = await screen.findByLabelText('Auto-refresh running jobs');
    expect(select).toHaveValue('15000');
    expect(within(select).getByRole('option', { name: 'Auto-refresh 15 s' })).toBeInTheDocument();
  });

  it('reports the API, PostgreSQL and Redis as online', async () => {
    shellApi().install();
    renderApp('/');
    expect(await screen.findByText('API online')).toBeInTheDocument();
  });

  it('reports a degraded dependency by name', async () => {
    shellApi()
      .on('GET', '/health/ready', () =>
        json(
          {
            status: 'not_ready',
            checks: {
              postgres: { status: 'ok', latency_ms: 2 },
              redis: { status: 'timeout', latency_ms: 2000 },
            },
            checked_at: '2026-11-07T10:00:00Z',
          },
          503,
        ),
      )
      .install();
    renderApp('/');
    expect(await screen.findByText('Degraded')).toBeInTheDocument();
    expect(screen.getByText(/Redis timeout/)).toBeInTheDocument();
  });

  it('reports an unreachable API', async () => {
    shellApi()
      .on('GET', '/health/ready', () => {
        throw new TypeError('Failed to fetch');
      })
      .on('GET', '/overview', () => {
        throw new TypeError('Failed to fetch');
      })
      .install();
    renderApp('/');
    expect(await screen.findByText('API unreachable')).toBeInTheDocument();
    expect(await screen.findByText('Cannot reach the SiteScope API')).toBeInTheDocument();
  });

  it('shows a not-found page for unknown addresses', async () => {
    shellApi().install();
    renderApp('/no/such/page');
    expect(await screen.findByText('Page not found')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to the overview' })).toHaveAttribute('href', '/');
  });

  it('offers a skip link to the content and opens the navigation drawer on small screens', async () => {
    shellApi().install();
    const { user } = renderApp('/');
    expect(await screen.findByRole('link', { name: 'Skip to content' })).toHaveAttribute(
      'href',
      '#main',
    );
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }));
    const drawer = screen.getByRole('dialog', { name: 'Navigation' });
    expect(drawer).toHaveAttribute('open');
    await user.click(within(drawer).getByRole('button', { name: 'Close navigation' }));
    expect(drawer).not.toHaveAttribute('open');
  });

  it('turns auto-refresh off, and remembers the choice', async () => {
    shellApi().install();
    const { user } = renderApp('/');
    await user.selectOptions(await screen.findByLabelText('Auto-refresh running jobs'), 'off');
    expect(window.localStorage.getItem('sitescope.pollIntervalMs')).toBe('off');
  });
});

describe('Overview', () => {
  it('shows totals from the overview endpoint, never from a page of jobs', async () => {
    const api = shellApi().install();
    renderApp('/');
    const summarySection = await screen.findByRole('region', { name: 'All crawl jobs' });
    const tile = (label: string) => within(summarySection).getByText(label).closest('div');
    expect(tile('Crawl jobs')).toHaveTextContent('4');
    expect(tile('Active jobs')).toHaveTextContent('1');
    expect(tile('Completed jobs')).toHaveTextContent('2');
    expect(tile('URLs admitted')).toHaveTextContent('52');
    expect(tile('Failed URLs')).toHaveTextContent('1');
    expect(tile('Pages stored')).toHaveTextContent('36');
    expect(tile('Indexed documents')).toHaveTextContent('91% of 34');
    expect(api.calls('/overview')).toHaveLength(1);
    const statusTable = screen.getByRole('table', { name: 'Crawl jobs by status' });
    expect(within(statusTable).getByRole('row', { name: /^Cancelled/ })).toHaveTextContent('1');
  });

  it('shows the newest job’s HTTP and depth distributions, and the recent jobs', async () => {
    shellApi().install();
    renderApp('/');
    expect(await screen.findByText(/Newest job:/)).toBeInTheDocument();
    expect(
      await screen.findByRole('table', { name: 'URLs of this job by final HTTP status class' }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('table', { name: 'Crawl jobs, newest first' }),
    ).toBeInTheDocument();
  });

  it('explains an overview that could not be counted', async () => {
    shellApi()
      .on('GET', '/overview', () =>
        problem(503, 'analysis_timeout', {
          detail: 'The overview took longer than 10 s and was stopped.',
        }),
      )
      .install();
    renderApp('/');
    expect(await screen.findByText('The analysis took too long')).toBeInTheDocument();
  });
});
