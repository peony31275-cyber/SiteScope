import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { JOB_ID, link, nth, PAGE_ID, settled, stats, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';

function analysisApi() {
  return new FakeApi()
    .on('GET', '/crawls', () => json({ items: [summary()], next_cursor: null }))
    .on('GET', '/crawls/:jobId', () => json(settled()))
    .on('GET', '/crawls/:jobId/stats', () => json(stats()));
}

describe('Site analysis: statistics', () => {
  it('shows the crawl counts the server returned', async () => {
    analysisApi().install();
    renderApp(`/analysis?job=${JOB_ID}`);
    const crawl = await screen.findByRole('region', { name: 'Crawl statistics' });
    const tile = (label: string) => within(crawl).getByText(label).closest('div');
    expect(tile('URLs discovered')).toHaveTextContent('15');
    expect(tile('URLs admitted')).toHaveTextContent('14');
    expect(tile('Pages stored')).toHaveTextContent('12');
    expect(tile('Crawl depth')).toHaveTextContent('3');
    expect(tile('Successful responses')).toHaveTextContent('11');
  });

  it('plots every HTTP status class the API returns, 1xx included', async () => {
    analysisApi().install();
    renderApp(`/analysis?job=${JOB_ID}`);
    const table = await screen.findByRole('table', { name: 'URLs by final HTTP status class' });
    const row = (label: string) =>
      within(table).getByRole('row', { name: new RegExp(`^${label}`) });
    expect(row('1xx informational')).toHaveTextContent('1');
    expect(row('2xx success')).toHaveTextContent('11');
    expect(row('4xx client error')).toHaveTextContent('1');
    expect(row('No response')).toHaveTextContent('0');
    expect(screen.getByRole('table', { name: /by BFS depth/ })).toHaveTextContent('Depth 1554');
  });

  it('labels link classes as verified or unverified, and only "Broken" as broken', async () => {
    analysisApi().install();
    renderApp(`/analysis?job=${JOB_ID}`);
    const table = await screen.findByRole('table', { name: 'Stored links by classification' });
    expect(within(table).getByRole('row', { name: /^Broken Verified/ })).toHaveTextContent('1');
    expect(
      within(table).getByRole('row', { name: /^Robots blocked Unverified/ }),
    ).toHaveTextContent('1');
    expect(within(table).getByRole('row', { name: /^Out of scope Unverified/ })).toHaveTextContent(
      '2',
    );
    expect(screen.getByText(/Unverified links are neither working nor broken/)).toBeInTheDocument();
  });

  it('explains an analysis that took too long', async () => {
    analysisApi()
      .on('GET', '/crawls/:jobId/stats', () =>
        problem(503, 'analysis_timeout', {
          detail: 'The analysis took longer than 10 s and was stopped.',
        }),
      )
      .install();
    renderApp(`/analysis?job=${JOB_ID}`);
    expect(await screen.findByText('The analysis took too long')).toBeInTheDocument();
    expect(screen.getByText(/took longer than 10 s/)).toBeInTheDocument();
  });

  it('reports a job that does not exist', async () => {
    analysisApi()
      .on('GET', '/crawls/:jobId', () =>
        problem(404, 'not_found', { detail: 'Crawl job x does not exist.' }),
      )
      .install();
    renderApp(`/analysis?job=${JOB_ID}`);
    expect(await screen.findByText('Crawl job x does not exist.')).toBeInTheDocument();
  });
});

describe('Site analysis: broken links', () => {
  it('lists only the confirmed broken links of /broken-links, with what verified them', async () => {
    const api = analysisApi()
      .on('GET', '/crawls/:jobId/broken-links', () =>
        json({
          total: 1,
          items: [
            {
              source_page_id: PAGE_ID,
              source_url: 'http://127.0.0.1:8001/about.html',
              anchor_text: '2024 roadmap',
              position: 3,
              target_url: 'http://127.0.0.1:8001/missing.html',
              status: 404,
              final_url: null,
              redirect_hops: 0,
              target_http_status: 404,
              checked_at: '2026-11-07T10:00:40Z',
            },
          ],
          next_cursor: null,
        }),
      )
      .install();
    renderApp(`/analysis?job=${JOB_ID}&tab=broken`);

    const table = await screen.findByRole('table', { name: 'Confirmed broken links' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('2024 roadmap')).toBeInTheDocument();
    expect(within(table).getByText('/missing.html')).toBeInTheDocument();
    expect(within(table).getByText('404 Not Found')).toBeInTheDocument();
    expect(within(table).getByText('Target answered 404')).toBeInTheDocument();
    expect(
      screen.getByText(
        /Server errors, refused requests \(401, 403\), and links the crawl never requested/,
      ),
    ).toBeInTheDocument();
    expect(api.calls(/\/links$/)).toHaveLength(0); // the broken list never comes from the links listing
  });

  it('says there is none, without counting unverified links', async () => {
    analysisApi()
      .on('GET', '/crawls/:jobId/broken-links', () =>
        json({ total: 0, items: [], next_cursor: null }),
      )
      .install();
    renderApp(`/analysis?job=${JOB_ID}&tab=broken`);
    expect(await screen.findByText('No confirmed broken link')).toBeInTheDocument();
  });
});

describe('Site analysis: links', () => {
  it('shows an unverified link as unverified, never as broken', async () => {
    analysisApi()
      .on('GET', '/crawls/:jobId/links', () => json({ items: [link()], next_cursor: null }))
      .install();
    renderApp(`/analysis?job=${JOB_ID}&tab=links`);
    const table = await screen.findByRole('table', {
      name: 'Stored links with their classification',
    });
    const row = nth(within(table).getAllByRole('row'), 1);
    expect(within(row).getByText('Robots blocked')).toBeInTheDocument();
    expect(within(row).getByText('unverified')).toBeInTheDocument();
    expect(within(row).getByText('not requested (skipped)')).toBeInTheDocument();
    expect(within(row).queryByText('Broken')).not.toBeInTheDocument();
  });

  it('sends the chosen filters, and a new filter starts from the first page', async () => {
    const api = analysisApi()
      .on('GET', '/crawls/:jobId/links', (request) =>
        json({ items: [link()], next_cursor: request.query.get('cursor') === null ? 'L2' : null }),
      )
      .install();
    const { user } = renderApp(`/analysis?job=${JOB_ID}&tab=links`);
    const pager = await screen.findByRole('navigation', { name: 'Link list pages' });
    await user.click(within(pager).getByRole('button', { name: /Next/ }));
    await waitFor(() => {
      expect(
        api
          .calls(/\/links$/)
          .at(-1)
          ?.query.get('cursor'),
      ).toBe('L2');
    });

    await user.selectOptions(screen.getByLabelText('Classification'), 'broken');
    await waitFor(() => {
      const last = api.calls(/\/links$/).at(-1);
      expect([last?.query.get('classification'), last?.query.get('cursor')]).toEqual([
        'broken',
        null,
      ]);
    });
    await user.selectOptions(screen.getByLabelText('Verification'), 'false');
    await waitFor(() => {
      expect(
        api
          .calls(/\/links$/)
          .at(-1)
          ?.query.get('verified'),
      ).toBe('false');
    });
  });

  it('opens a class from the statistics as a filtered listing', async () => {
    const api = analysisApi()
      .on('GET', '/crawls/:jobId/links', () => json({ items: [], next_cursor: null }))
      .install();
    const { user } = renderApp(`/analysis?job=${JOB_ID}`);
    const table = await screen.findByRole('table', { name: 'Stored links by classification' });
    await user.click(within(table).getByRole('link', { name: 'Robots blocked' }));
    await waitFor(() => {
      expect(
        api
          .calls(/\/links$/)
          .at(-1)
          ?.query.get('classification'),
      ).toBe('robots_blocked');
    });
    expect(screen.getByRole('tab', { name: 'Links' })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('Site analysis: pages and tabs', () => {
  it('walks the page listing with opaque cursors, back and forth', async () => {
    const page = (id: string, path: string) => ({
      page_id: id,
      url: `http://127.0.0.1:8001${path}`,
      title: `Page ${path}`,
      description: null,
      http_status: 200,
      content_type: 'text/html',
      depth: 1,
      crawled_at: '2026-11-07T10:00:30Z',
      noindex: false,
      nofollow: false,
      index_status: 'indexed' as const,
      indexed_at: '2026-11-07T10:00:31Z',
    });
    const api = analysisApi()
      .on('GET', '/crawls/:jobId/pages', (request) =>
        request.query.get('cursor') === 'WyJwYWdlcyIsMSwiL2EiXQ'
          ? json({ items: [page('0199a1b2-8d4e-7f50-9b0c-000000000002', '/b')], next_cursor: null })
          : json({ items: [page(PAGE_ID, '/a')], next_cursor: 'WyJwYWdlcyIsMSwiL2EiXQ' }),
      )
      .install();
    const { user } = renderApp(`/analysis?job=${JOB_ID}&tab=pages`);
    const pager = await screen.findByRole('navigation', { name: 'Page list pages' });
    await user.click(within(pager).getByRole('button', { name: /Next/ }));
    expect(await screen.findByText('Page /b')).toBeInTheDocument();
    // The cursor goes back exactly as the server sent it.
    expect(
      api
        .calls(/\/pages$/)
        .at(-1)
        ?.query.get('cursor'),
    ).toBe('WyJwYWdlcyIsMSwiL2EiXQ');
    await user.click(within(pager).getByRole('button', { name: /Previous/ }));
    expect(await screen.findByText('Page /a')).toBeInTheDocument();
  });

  it('moves between tabs with the arrow keys', async () => {
    analysisApi()
      .on('GET', '/crawls/:jobId/pages', () => json({ items: [], next_cursor: null }))
      .install();
    const { user } = renderApp(`/analysis?job=${JOB_ID}`);
    const statistics = await screen.findByRole('tab', { name: 'Statistics' });
    statistics.focus();
    await user.keyboard('{ArrowRight}');
    const pages = screen.getByRole('tab', { name: 'Pages' });
    expect(pages).toHaveFocus();
    expect(pages).toHaveAttribute('aria-selected', 'true');
    expect(pages).toHaveAttribute('aria-controls', 'analysis-panel-pages');
    expect(statistics).not.toHaveAttribute('aria-controls');
    expect(document.getElementById('analysis-panel-pages')).not.toBeNull();
    expect(await screen.findByText('No stored pages match')).toBeInTheDocument();
  });
});
