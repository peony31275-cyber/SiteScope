import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { indexing, JOB_ID, searchResponse, searchResult, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';

function searchApi(respond: Parameters<FakeApi['on']>[2]) {
  return new FakeApi()
    .on('GET', '/crawls', () => json({ items: [summary()], next_cursor: null }))
    .on('GET', '/search', respond)
    .install();
}

describe('Search explorer', () => {
  it('never shows another job’s results while the selected job’s load (review finding)', async () => {
    const other = '0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a6c';
    new FakeApi()
      .on('GET', '/crawls', () =>
        json({
          items: [summary(), summary({ id: other, seed_url: 'http://127.0.0.2:8002/' })],
          next_cursor: null,
        }),
      )
      .on(
        'GET',
        '/search',
        (request) =>
          request.query.get('job_id') === JOB_ID
            ? json(searchResponse({ results: [searchResult({ title: 'Only in job A' })] }))
            : new Promise<Response>(() => undefined), // job B's search is still running
      )
      .install();
    const { user } = renderApp(`/search?job=${JOB_ID}&q=fencing`);
    expect(await screen.findByText('Only in job A')).toBeInTheDocument();
    const selector = screen.getByLabelText('Crawl job');
    await waitFor(() => {
      expect(within(selector).getAllByRole('option')).toHaveLength(3);
    });
    await user.selectOptions(selector, other);
    expect(await screen.findByText('Searching…')).toBeInTheDocument();
    expect(screen.queryByText('Only in job A')).not.toBeInTheDocument();
    expect(screen.queryByText(/1 result for/)).not.toBeInTheDocument();
  });

  it('asks for a job first', async () => {
    new FakeApi().install();
    renderApp('/search');
    expect(await screen.findByText('Select a crawl job')).toBeInTheDocument();
  });

  it('searches the selected job and shows ranked results with highlighted words', async () => {
    const api = searchApi(() => json(searchResponse()));
    const { user } = renderApp(`/search?job=${JOB_ID}`);

    await user.type(await screen.findByLabelText('Search query'), 'fencing');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    const results = await screen.findByRole('region', { name: 'Search results' });
    const [request] = api.calls('/search');
    expect(Object.fromEntries(request?.query ?? [])).toEqual({
      job_id: JOB_ID,
      q: 'fencing',
      page: '1',
      page_size: '10',
    });
    expect(results).toHaveTextContent(/1 result for “fencing”/);
    const mark = within(results).getByText('fencing', { selector: 'mark' });
    expect(mark).toHaveClass('hit');
    const title = within(results).getByRole('link', { name: /Architecture/ });
    expect(title).toHaveAttribute('href', 'http://127.0.0.1:8001/docs/architecture.html');
    expect(title).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(title).toHaveAttribute('target', '_blank');
    expect(within(results).getByText('score 0.6079')).toBeInTheDocument();
    expect(screen.getByLabelText('Crawl job')).toHaveValue(JOB_ID);
  });

  it('marks words after an emoji at the right code points', async () => {
    searchApi(() =>
      json(
        searchResponse({
          results: [
            searchResult({ snippet: '😀 crawl politely', highlights: [{ start: 2, end: 7 }] }),
          ],
        }),
      ),
    );
    renderApp(`/search?job=${JOB_ID}&q=crawl`);
    expect(await screen.findByText('crawl', { selector: 'mark' })).toBeInTheDocument();
  });

  it('renders snippets and titles as text: markup in crawled content is never HTML', async () => {
    searchApi(() =>
      json(
        searchResponse({
          results: [
            searchResult({
              title: '<script>alert(1)</script>',
              snippet: '<img src=x onerror=alert(1)> fencing',
              highlights: [{ start: 30, end: 37 }],
              url: 'javascript:alert(1)',
            }),
          ],
        }),
      ),
    );
    const { container } = renderApp(`/search?job=${JOB_ID}&q=fencing`);
    expect(
      await screen.findByText('<img src=x onerror=alert(1)>', { exact: false }),
    ).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    // A non-http(s) URL is not a link.
    expect(screen.queryByRole('link', { name: /script/ })).not.toBeInTheDocument();
    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument();
  });

  it('refuses an empty query without asking the server', async () => {
    const api = searchApi(() => json(searchResponse()));
    const { user } = renderApp(`/search?job=${JOB_ID}`);
    await user.type(await screen.findByLabelText('Search query'), '   ');
    await user.click(screen.getByRole('button', { name: 'Search' }));
    expect(screen.getByText('Enter the words to search for.')).toBeInTheDocument();
    expect(api.calls('/search')).toHaveLength(0);
  });

  it('explains a query with nothing to search for (stop words only)', async () => {
    searchApi(() =>
      json(searchResponse({ query: 'the and', searchable: false, total: 0, results: [] })),
    );
    renderApp(`/search?job=${JOB_ID}&q=the%20and`);
    expect(await screen.findByText('Nothing to search for')).toBeInTheDocument();
  });

  it('says when nothing matches', async () => {
    searchApi(() => json(searchResponse({ query: 'zebra', total: 0, results: [] })));
    renderApp(`/search?job=${JOB_ID}&q=zebra`);
    expect(await screen.findByText(/No indexed page matches/)).toBeInTheDocument();
  });

  it('pages through results by number, never past the deepest page the server serves', async () => {
    const api = searchApi((request) =>
      json(
        searchResponse({
          total: 5000,
          page: Number(request.query.get('page')),
          page_size: 50,
          results: [
            searchResult({
              page_id: `0199a1b2-8d4e-7f50-9b0c-${request.query.get('page')?.padStart(12, '0') ?? ''}`,
            }),
          ],
        }),
      ),
    );
    const { user } = renderApp(`/search?job=${JOB_ID}&q=fencing&size=50`);
    const pages = await screen.findByRole('navigation', { name: 'Search result pages' });
    // 1000 / 50 + 1 = 21 pages are served, although 5000 results match.
    expect(within(pages).getByRole('button', { name: 'Page 21' })).toBeInTheDocument();
    expect(within(pages).queryByRole('button', { name: 'Page 100' })).not.toBeInTheDocument();
    expect(screen.getByText(/serves the first 1,050 results/)).toBeInTheDocument();

    await user.click(within(pages).getByRole('button', { name: 'Page 2' }));
    await waitFor(() => {
      expect(api.calls('/search').at(-1)?.query.get('page')).toBe('2');
    });
    expect(await screen.findByText(/showing 51–51/)).toBeInTheDocument();
  });

  it('shows the server’s validation errors for the query and the page together', async () => {
    searchApi(() =>
      problem(422, 'validation_failed', {
        detail: 'The request failed validation.',
        errors: [
          {
            loc: ['query', 'q'],
            msg: 'the query is longer than 256 characters',
            type: 'query_too_long',
          },
          {
            loc: ['query', 'page'],
            msg: 'page must be at most 101 for page_size 10',
            type: 'page_out_of_range',
          },
        ],
      }),
    );
    renderApp(`/search?job=${JOB_ID}&q=x&page=500`);
    expect(
      await screen.findByText('q: the query is longer than 256 characters'),
    ).toBeInTheDocument();
    expect(screen.getByText('page: page must be at most 101 for page_size 10')).toBeInTheDocument();
  });

  it('explains a search timeout', async () => {
    searchApi(() =>
      problem(503, 'search_timeout', {
        detail: 'The search took longer than 5 s and was stopped.',
      }),
    );
    renderApp(`/search?job=${JOB_ID}&q=fencing`);
    expect(await screen.findByText('The search took too long')).toBeInTheDocument();
    expect(screen.getByText(/Try a more specific query/)).toBeInTheDocument();
  });

  it('notes that more pages may become searchable while indexing runs', async () => {
    searchApi(() => json(searchResponse({ indexing: indexing() })));
    renderApp(`/search?job=${JOB_ID}&q=fencing`);
    expect(await screen.findByText('Indexing is still in progress')).toBeInTheDocument();
    expect(screen.getByText(/2 of 4 pages are indexed/)).toBeInTheDocument();
  });
});
