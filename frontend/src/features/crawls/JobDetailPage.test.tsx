import { act, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { detail, indexing, JOB_ID, OTHER_JOB_ID, settled, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';

describe('Crawl job detail', () => {
  it('shows the crawl and its indexing as separate statuses, with the counts the server read', async () => {
    new FakeApi().on('GET', '/crawls/:jobId', () => json(detail())).install();
    renderApp(`/crawls/${JOB_ID}`);

    expect(await screen.findByRole('region', { name: 'Crawl job' })).toBeInTheDocument();
    const header = screen.getByRole('region', { name: 'Crawl job' });
    expect(within(header).getByText('Running')).toBeInTheDocument();
    expect(within(header).getByText('Indexing running')).toBeInTheDocument();
    expect(
      screen.getByText('Crawling depth 1 of 3; 2 of 4 pages indexed so far.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'URLs finished' })).toHaveAttribute(
      'aria-valuenow',
      '4',
    );
    expect(screen.getByRole('progressbar', { name: 'Page budget used' })).toHaveAttribute(
      'aria-valuemax',
      '100',
    );
    expect(
      screen.getByRole('img', {
        name: /Search documents by status: 2 Indexed, 1 Queued, 1 Pending, 0 Failed/,
      }),
    ).toBeInTheDocument();
  });

  it('distinguishes a completed crawl whose indexing still runs from a fully indexed one', async () => {
    let job = detail({ status: 'completed', finished_at: '2026-11-07T10:01:00Z' });
    new FakeApi().on('GET', '/crawls/:jobId', () => json(job)).install();
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    expect(
      await screen.findByText(
        'Crawl completed · indexing not complete yet (2 of 4 pages indexed).',
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Crawl job' })).getByText('Indexing running'),
    ).toBeInTheDocument();

    job = settled();
    await user.click(screen.getByRole('button', { name: 'Refresh data now' }));
    expect(
      await screen.findByText('Crawl completed · fully indexed (11 of 11 pages indexed).'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Final state: the crawl and its indexing are over/),
    ).toBeInTheDocument();
  });

  it('reports indexing failures without calling the crawl failed', async () => {
    new FakeApi()
      .on('GET', '/crawls/:jobId', () =>
        json(
          settled({
            indexing: indexing({
              status: 'failed',
              documents_total: 11,
              documents_by_status: { pending: 0, queued: 0, indexed: 10, failed: 1 },
              active: 0,
              completed_at: '2026-11-07T10:01:05Z',
            }),
          }),
        ),
      )
      .install();
    renderApp(`/crawls/${JOB_ID}`);
    expect(
      await screen.findByText(
        'Crawl completed · indexing failed for 1 pages (10 of 11 pages indexed).',
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText('Indexing failed').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0);
  });

  it('explains a failed crawl with the server’s reason', async () => {
    new FakeApi()
      .on('GET', '/crawls/:jobId', () =>
        json(settled({ status: 'failed', failure_reason: 'seed_unreachable' })),
      )
      .install();
    renderApp(`/crawls/${JOB_ID}`);
    expect(await screen.findByText('The crawl failed')).toBeInTheDocument();
    expect(screen.getByText('seed_unreachable')).toBeInTheDocument();
  });

  it('says when the job does not exist, and when the address holds no job ID', async () => {
    new FakeApi()
      .on('GET', '/crawls/:jobId', () =>
        problem(404, 'not_found', { detail: 'Crawl job x does not exist.' }),
      )
      .install();
    renderApp(`/crawls/${JOB_ID}`);
    expect(await screen.findByText('Crawl job not found')).toBeInTheDocument();

    const api = new FakeApi().install();
    renderApp('/crawls/not-a-uuid');
    expect(await screen.findByText('This is not a crawl job ID')).toBeInTheDocument();
    expect(api.calls(/\/crawls\/not-a-uuid/)).toHaveLength(0);
  });
});

describe('Cancelling a crawl', () => {
  function cancellable(response: () => Response) {
    let current = detail();
    const api = new FakeApi()
      .on('GET', '/crawls/:jobId', () => json(current))
      .on('POST', '/crawls/:jobId/cancel', () => {
        const answer = response();
        return answer;
      })
      .install();
    return {
      api,
      setJob: (job: typeof current) => {
        current = job;
      },
    };
  }

  it('asks for confirmation, and keeping the crawl sends nothing', async () => {
    const { api } = cancellable(() => json(summary({ status: 'cancelled' })));
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));

    const dialog = screen.getByRole('dialog', {
      name: 'Cancel crawl job …2e3f4a5b of 127.0.0.1:8001?',
    });
    expect(within(dialog).getByRole('button', { name: 'Keep crawling' })).toHaveFocus();
    expect(dialog).toHaveAccessibleDescription(/Pages already stored are kept/);
    await user.click(within(dialog).getByRole('button', { name: 'Keep crawling' }));
    expect(dialog).not.toHaveAttribute('open');
    expect(api.calls(/cancel$/, 'POST')).toHaveLength(0);
  });

  it('shows "cancelling" from a 202 and explains that in-flight URLs are settling', async () => {
    const { api, setJob } = cancellable(() =>
      json(
        summary({
          status: 'cancelling',
          cancel_requested_at: '2026-11-07T10:00:20Z',
          updated_at: '2026-11-07T10:00:20Z',
        }),
        202,
      ),
    );
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    setJob(
      detail({
        status: 'cancelling',
        cancel_requested_at: '2026-11-07T10:00:20Z',
        updated_at: '2026-11-07T10:00:20Z',
      }),
    );
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));

    expect(await screen.findByText('Cancellation requested')).toBeInTheDocument();
    expect(await screen.findByText('Cancellation is settling')).toBeInTheDocument();
    expect(screen.getByText(/1 URL is still being fetched/)).toBeInTheDocument();
    const header = screen.getByRole('region', { name: 'Crawl job' });
    expect(within(header).getByText('Cancelling')).toBeInTheDocument();
    expect(within(header).queryByText('Cancelled')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel crawl' })).not.toBeInTheDocument();
    expect(api.calls(/cancel$/, 'POST')).toHaveLength(1);
  });

  it('never shows "cancelled" before the server says so: a job that ended first stays completed', async () => {
    const { setJob } = cancellable(() =>
      json(
        summary({
          status: 'completed',
          updated_at: '2026-11-07T10:01:00Z',
          finished_at: '2026-11-07T10:01:00Z',
        }),
      ),
    );
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    setJob(settled());
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));

    expect(await screen.findByText('The crawl had already completed')).toBeInTheDocument();
    expect(screen.getByText(/nothing was changed/)).toBeInTheDocument();
    const header = screen.getByRole('region', { name: 'Crawl job' });
    await waitFor(() => {
      expect(within(header).getByText('Completed')).toBeInTheDocument();
    });
    expect(within(header).queryByText('Cancelled')).not.toBeInTheDocument();
  });

  it('confirms a cancellation the server completed at once', async () => {
    const { setJob } = cancellable(() =>
      json(
        summary({
          status: 'cancelled',
          finished_at: '2026-11-07T10:00:20Z',
          updated_at: '2026-11-07T10:00:20Z',
        }),
      ),
    );
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    setJob(
      detail({
        status: 'cancelled',
        finished_at: '2026-11-07T10:00:20Z',
        updated_at: '2026-11-07T10:00:20Z',
      }),
    );
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));
    expect(await screen.findByText('Crawl cancelled')).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Crawl job' })).getByText('Cancelled'),
    ).toBeInTheDocument();
  });

  it('reports a failed cancellation as an error and keeps the job as it was', async () => {
    cancellable(() => problem(503, 'dependency_unavailable'));
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));
    expect(
      await screen.findByText('Cancellation failed: A backing service is unavailable'),
    ).toBeInTheDocument();
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
  });

  it('never carries an open dialog over to another job (review finding)', async () => {
    const api = new FakeApi()
      .on('GET', '/crawls/:jobId', (_, { jobId }) =>
        json(
          detail({
            id: jobId,
            seed_url: jobId === JOB_ID ? 'http://127.0.0.1:8001/' : 'http://127.0.0.2:8002/',
          }),
        ),
      )
      .on('POST', '/crawls/:jobId/cancel', (_, { jobId }) =>
        json(summary({ id: jobId, status: 'cancelled' })),
      )
      .install();
    // Job B is visited first, so its detail is cached (the jobs table caches every row's too):
    // coming back to it renders at once, without a loading state in between.
    const { user, router } = renderApp(`/crawls/${OTHER_JOB_ID}`);
    await screen.findByRole('button', { name: 'Cancel crawl' });
    await act(async () => {
      await router.navigate(`/crawls/${JOB_ID}`);
    });
    await waitFor(() => {
      expect(screen.getByRole('region', { name: 'Crawl job' })).toHaveTextContent(JOB_ID);
    });
    await user.click(screen.getByRole('button', { name: 'Cancel crawl' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName(
      'Cancel crawl job …2e3f4a5b of 127.0.0.1:8001?',
    );

    // The browser's back button while the dialog is open.
    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() => {
      expect(screen.getByRole('region', { name: 'Crawl job' })).toHaveTextContent(OTHER_JOB_ID);
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.calls(/cancel$/, 'POST')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Cancel crawl' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName(
      'Cancel crawl job …2e3f4a6c of 127.0.0.2:8002?',
    );
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));
    await waitFor(() => {
      expect(api.calls(/cancel$/, 'POST').map((call) => call.path)).toEqual([
        `/api/v1/crawls/${OTHER_JOB_ID}/cancel`,
      ]);
    });
  });

  it('moves focus to the job’s state once the cancellation is confirmed', async () => {
    const { setJob } = cancellable(() =>
      json(
        summary({
          status: 'cancelled',
          finished_at: '2026-11-07T10:00:20Z',
          updated_at: '2026-11-07T10:00:20Z',
        }),
      ),
    );
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    setJob(
      detail({
        status: 'cancelled',
        finished_at: '2026-11-07T10:00:20Z',
        updated_at: '2026-11-07T10:00:20Z',
      }),
    );
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));
    await screen.findByText('Crawl cancelled');
    await waitFor(() => {
      expect(document.activeElement?.tagName).toBe('P');
    });
    expect(document.activeElement).toHaveTextContent(/^Crawl cancelled · /);
  });

  it('does not claim that a failed action is retried automatically', async () => {
    cancellable(() => {
      throw new TypeError('Failed to fetch');
    });
    const { user } = renderApp(`/crawls/${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Cancel crawl' }));
    await user.click(screen.getByRole('button', { name: 'Cancel the crawl' }));
    const toast = await screen.findByText('Cancellation failed: Cannot reach the SiteScope API');
    expect(toast.parentElement).toHaveTextContent(/then try again/);
    expect(toast.parentElement).not.toHaveTextContent(/retries/);
  });

  it('offers no cancellation for a finished job', async () => {
    new FakeApi().on('GET', '/crawls/:jobId', () => json(settled())).install();
    renderApp(`/crawls/${JOB_ID}`);
    await screen.findByRole('region', { name: 'Crawl job' });
    expect(screen.queryByRole('button', { name: 'Cancel crawl' })).not.toBeInTheDocument();
  });
});
