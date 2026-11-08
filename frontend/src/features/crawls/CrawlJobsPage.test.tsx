import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { detail, JOB_ID, nth, OTHER_JOB_ID, policy, settled, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';

function jobsApi() {
  return new FakeApi()
    .on('GET', '/crawl-policy', () => json(policy()))
    .on('GET', '/crawls/:jobId', (_, { jobId }) => json(detail({ id: jobId })));
}

describe('Create crawl job', () => {
  it('offers the approved targets and the server’s limits', async () => {
    jobsApi().install();
    const { user } = renderApp('/crawls');

    const seed = await screen.findByLabelText('Seed URL');
    expect(seed).toHaveValue('http://127.0.0.1/');
    await user.click(screen.getByRole('button', { name: 'http://127.0.0.1:8001/' }));
    expect(seed).toHaveValue('http://127.0.0.1:8001/');
    expect(screen.getByLabelText('Maximum BFS depth')).toHaveAttribute('max', '5');
    expect(screen.getByLabelText('Page limit')).toHaveAttribute('max', '1000');
    expect(screen.getByText(/a \* pattern approves subdomains/)).toBeInTheDocument();
  });

  it('reports invalid values and sends nothing', async () => {
    const api = jobsApi().install();
    const { user } = renderApp('/crawls');
    const seed = await screen.findByLabelText('Seed URL');

    await user.clear(seed);
    await user.type(seed, 'ftp://127.0.0.1/');
    const depth = screen.getByLabelText('Maximum BFS depth');
    await user.clear(depth);
    await user.type(depth, '9');
    const pages = screen.getByLabelText('Page limit');
    await user.clear(pages);
    await user.type(pages, '0');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));

    expect(screen.getByText(/The scheme "ftp" is not allowed on this server/)).toBeInTheDocument();
    expect(
      screen.getByText('The maximum depth must be between 0 and 5 on this server.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('The page limit must be between 1 and 1000 on this server.'),
    ).toBeInTheDocument();
    expect(seed).toHaveAttribute('aria-invalid', 'true');
    expect(seed).toHaveAccessibleDescription(/scheme "ftp" is not allowed/);

    await user.clear(seed);
    await user.type(seed, 'http://evil.test/');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    expect(
      screen.getByText('"evil.test" is not an approved crawl target on this server.'),
    ).toBeInTheDocument();
    expect(api.calls('/crawls', 'POST')).toHaveLength(0);
  });

  it('never rounds or clamps a value: a fraction is refused, not changed', async () => {
    const api = jobsApi().install();
    const { user } = renderApp('/crawls');
    const depth = await screen.findByLabelText('Maximum BFS depth');
    await user.clear(depth);
    await user.type(depth, '2.5');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    expect(screen.getByText('The maximum depth must be a whole number.')).toBeInTheDocument();
    expect(api.calls('/crawls', 'POST')).toHaveLength(0);
  });

  it('sends exactly the values entered, with an idempotency key, and opens the new job', async () => {
    const api = jobsApi()
      .on('POST', '/crawls', (request) =>
        json(summary({ id: OTHER_JOB_ID, status: 'queued', ...(request.body as object) }), 201),
      )
      .install();
    const { user } = renderApp('/crawls');
    const seed = await screen.findByLabelText('Seed URL');
    await user.clear(seed);
    await user.type(seed, ' http://127.0.0.1:8001/docs/ ');
    const depth = screen.getByLabelText('Maximum BFS depth');
    await user.clear(depth);
    await user.type(depth, '3');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Crawl job' })).toBeInTheDocument();
    const [post] = api.calls('/crawls', 'POST');
    expect(post?.body).toEqual({
      seed_url: 'http://127.0.0.1:8001/docs/',
      max_depth: 3,
      max_pages: 100,
    });
    expect(post?.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{32,36}$/);
    expect(await screen.findByText('Crawl job created')).toBeInTheDocument();
  });

  it('shows the server’s refusal on the field it names', async () => {
    jobsApi()
      .on('POST', '/crawls', () =>
        problem(422, 'validation_failed', {
          detail: 'The request failed validation.',
          errors: [
            {
              loc: ['body', 'seed_url'],
              msg: "host '127.0.0.1' is not an approved crawl target",
              type: 'target_not_allowed',
            },
          ],
        }),
      )
      .install();
    const { user } = renderApp('/crawls');
    await screen.findByLabelText('Seed URL');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));

    expect(
      await screen.findByText("host '127.0.0.1' is not an approved crawl target"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Seed URL')).toHaveAttribute('aria-invalid', 'true');
  });

  it('retries an uncertain submission with the same idempotency key, and a changed one with a new key', async () => {
    let attempts = 0;
    const api = jobsApi()
      .on('POST', '/crawls', () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError('Failed to fetch');
        return json(summary({ id: OTHER_JOB_ID, status: 'queued' }), 200); // a replay of attempt 1
      })
      .install();
    const { user } = renderApp('/crawls');
    await screen.findByLabelText('Seed URL');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    expect(
      await screen.findByText('The request may not have reached the server'),
    ).toBeInTheDocument();
    expect(screen.getByText(/Submitting again is safe/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    expect(await screen.findByText('Crawl job already created')).toBeInTheDocument();
    const [first, second] = api.calls('/crawls', 'POST');
    expect(second?.headers.get('Idempotency-Key')).toBe(first?.headers.get('Idempotency-Key'));
  });

  it('keeps the idempotency key through edits that change nothing (review finding)', async () => {
    let attempts = 0;
    const api = jobsApi()
      .on('POST', '/crawls', () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError('Failed to fetch');
        return json(summary({ id: OTHER_JOB_ID, status: 'queued' }), 200);
      })
      .install();
    const { user } = renderApp('/crawls');
    const seed = await screen.findByLabelText('Seed URL');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    await screen.findByText('The request may not have reached the server');

    // Pick the target already chosen, then type a character and delete it.
    await user.click(screen.getByRole('button', { name: 'http://127.0.0.1/' }));
    await user.type(seed, 'x{Backspace}');
    expect(screen.getByText(/Submitting again is safe/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    await screen.findByText('Crawl job already created');
    const [first, second] = api.calls('/crawls', 'POST');
    expect(second?.headers.get('Idempotency-Key')).toBe(first?.headers.get('Idempotency-Key'));
  });

  it('moves focus to the first invalid field, so its error is read out', async () => {
    jobsApi().install();
    const { user } = renderApp('/crawls');
    const depth = await screen.findByLabelText('Maximum BFS depth');
    await user.clear(depth);
    await user.type(depth, '99');
    await user.click(screen.getByRole('button', { name: 'Create crawl job' }));
    expect(depth).toHaveFocus();
    expect(depth).toHaveAccessibleDescription(
      'The maximum depth must be between 0 and 5 on this server.',
    );
  });

  it('says so when no target is approved', async () => {
    new FakeApi().on('GET', '/crawl-policy', () => json(policy({ approved_hosts: [] }))).install();
    renderApp('/crawls');
    expect(
      await screen.findByText('No crawl target is approved on this server'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create crawl job' })).not.toBeInTheDocument();
  });
});

describe('Crawl job listing', () => {
  it('shows each job with the progress read from its detail', async () => {
    jobsApi()
      .on('GET', '/crawls', () =>
        json({
          items: [summary(), summary({ id: OTHER_JOB_ID, status: 'completed' })],
          next_cursor: null,
        }),
      )
      .on('GET', '/crawls/:jobId', (_, { jobId }) =>
        json(jobId === JOB_ID ? detail() : settled({ id: OTHER_JOB_ID })),
      )
      .install();
    renderApp('/crawls');

    const table = await screen.findByRole('table', { name: 'Crawl jobs, newest first' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    await waitFor(() => {
      expect(within(nth(rows, 1)).getByText('Fully indexed')).toBeInTheDocument();
    });
    expect(within(nth(rows, 0)).getByText('Running')).toBeInTheDocument();
    expect(within(nth(rows, 0)).getByText('Indexing running')).toBeInTheDocument();
    expect(within(nth(rows, 1)).getByText('12')).toBeInTheDocument(); // pages stored
  });

  it('pages with the server’s cursors: next sends the cursor, previous returns to the first page', async () => {
    const api = jobsApi()
      .on('GET', '/crawls', (request) =>
        request.query.get('cursor') === 'c2'
          ? json({
              items: [summary({ id: OTHER_JOB_ID, seed_url: 'http://127.0.0.1:8001/second' })],
              next_cursor: null,
            })
          : json({ items: [summary()], next_cursor: 'c2' }),
      )
      .install();
    const { user } = renderApp('/crawls');
    const pager = await screen.findByRole('navigation', { name: 'Crawl job pages' });

    await user.click(within(pager).getByRole('button', { name: /Next/ }));
    expect(await screen.findByText('http://127.0.0.1:8001/second')).toBeInTheDocument();
    expect(within(pager).getByText('Page 2')).toBeInTheDocument();
    expect(api.calls('/crawls').some((call) => call.query.get('cursor') === 'c2')).toBe(true);
    expect(within(pager).getByRole('button', { name: /Next/ })).toBeDisabled();

    await user.click(within(pager).getByRole('button', { name: /Previous/ }));
    expect(await screen.findByText('Page 1')).toBeInTheDocument();
    expect(screen.queryByText('http://127.0.0.1:8001/second')).not.toBeInTheDocument();
  });

  it('starts again from the first page when the page size changes', async () => {
    const api = jobsApi()
      .on('GET', '/crawls', (request) =>
        json({
          items: [summary()],
          next_cursor: request.query.get('cursor') === null ? 'c2' : null,
        }),
      )
      .install();
    const { user } = renderApp('/crawls');
    const pager = await screen.findByRole('navigation', { name: 'Crawl job pages' });
    await user.click(within(pager).getByRole('button', { name: /Next/ }));
    await screen.findByText('Page 2');
    await user.selectOptions(screen.getByLabelText('Jobs per page'), '20');
    await waitFor(() => {
      const last = api.calls('/crawls').at(-1);
      expect([last?.query.get('limit'), last?.query.get('cursor')]).toEqual(['20', null]);
    });
  });

  it('explains an empty deployment', async () => {
    jobsApi().install();
    renderApp('/crawls');
    expect(await screen.findByText('No crawl jobs yet')).toBeInTheDocument();
  });

  it('shows an outage with a retry, and recovers', async () => {
    let fail = true;
    jobsApi()
      .on('GET', '/crawls', () =>
        fail
          ? problem(503, 'dependency_unavailable')
          : json({ items: [summary()], next_cursor: null }),
      )
      .install();
    const { user } = renderApp('/crawls');
    expect(await screen.findByText('A backing service is unavailable')).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(
      await screen.findByRole('table', { name: 'Crawl jobs, newest first' }),
    ).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
