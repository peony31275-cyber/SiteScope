import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeApi, json, problem } from '../../tests/fakeApi';
import { graph, JOB_ID, summary } from '../../tests/fixtures';
import { renderApp } from '../../tests/render';

function graphApi(respond: Parameters<FakeApi['on']>[2] = () => json(graph())) {
  return new FakeApi()
    .on('GET', '/crawls', () => json({ items: [summary()], next_cursor: null }))
    .on('GET', '/crawls/:jobId/link-graph', respond)
    .install();
}

describe('Link explorer', () => {
  it('requests the graph with conservative limits and draws every node kind', async () => {
    const api = graphApi();
    const { container } = renderApp(`/graph?job=${JOB_ID}`);
    expect(
      await screen.findByRole('figure', { name: /Internal link graph: 4 nodes and 3 links shown/ }),
    ).toBeInTheDocument();
    const [request] = api.calls(`/crawls/${JOB_ID}/link-graph`);
    expect([request?.query.get('max_nodes'), request?.query.get('max_edges')]).toEqual([
      '300',
      '1500',
    ]);
    expect(container.querySelectorAll('[data-node]')).toHaveLength(4);
    expect(
      screen.getByText(/The whole graph: 4 nodes and 3 stored internal links/),
    ).toBeInTheDocument();
    const legend = screen.getByRole('list', { name: 'Legend' });
    expect(within(legend).getByText('Never admitted')).toBeInTheDocument();
  });

  it('shows what the API says about a node selected in the list, and only that', async () => {
    graphApi();
    const { user } = renderApp(`/graph?job=${JOB_ID}`);
    const list = await screen.findByRole('list', { name: 'Graph nodes' });
    await user.click(within(list).getByRole('button', { name: /\/missing\.html/ }));

    const details = screen.getByRole('region', { name: 'Node details' });
    expect(within(details).getByText('Admitted URL without a page')).toBeInTheDocument();
    expect(within(details).getByText('404')).toBeInTheDocument();
    expect(within(details).getByText('not a page: no links known')).toBeInTheDocument();
    expect(within(list).getByRole('button', { name: /\/missing\.html/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('never calls a node without inbound links an orphan', async () => {
    graphApi();
    const { user } = renderApp(`/graph?job=${JOB_ID}`);
    const list = await screen.findByRole('list', { name: 'Graph nodes' });
    await user.click(within(list).getByRole('button', { name: /legacy\.html/ }));
    const details = screen.getByRole('region', { name: 'Node details' });
    expect(
      within(details).getByText(/this does not mean that nothing links to it/),
    ).toBeInTheDocument();
    expect(within(details).queryByText(/orphan/i)).not.toBeInTheDocument();
    expect(within(details).getAllByText('never admitted').length).toBeGreaterThan(0);
  });

  it('selects a node by clicking it in the drawing', async () => {
    graphApi();
    const { container, user } = renderApp(`/graph?job=${JOB_ID}`);
    await screen.findByRole('figure');
    const about = container.querySelector('[data-node="1"] circle');
    if (about === null) throw new Error('node 1 not drawn');
    await user.pointer([
      { keys: '[MouseLeft>]', target: about },
      { keys: '[/MouseLeft]', target: about },
    ]);
    const details = screen.getByRole('region', { name: 'Node details' });
    expect(within(details).getByText('About')).toBeInTheDocument();
  });

  it('warns when the graph was cut at the limits, with the real totals', async () => {
    graphApi(() =>
      json(
        graph({
          truncated: true,
          total_nodes: 1234,
          total_edges: 9876,
          max_nodes: 4,
          max_edges: 3,
        }),
      ),
    );
    renderApp(`/graph?job=${JOB_ID}`);
    const warning = await screen.findByText('Partial graph: cut at the limits');
    expect(warning.parentElement).toHaveTextContent(
      'Showing 4 of 1,234 nodes and 3 of 9,876 links',
    );
  });

  it('validates the limits before asking for a larger graph', async () => {
    const api = graphApi();
    const { user } = renderApp(`/graph?job=${JOB_ID}`);
    const nodes = await screen.findByLabelText('Most nodes');
    await user.clear(nodes);
    await user.type(nodes, '3000');
    await user.click(screen.getByRole('button', { name: 'Apply limits' }));
    expect(screen.getByText('Between 1 and 2,000.')).toBeInTheDocument();
    expect(api.calls(`/crawls/${JOB_ID}/link-graph`)).toHaveLength(1);

    await user.clear(nodes);
    await user.type(nodes, '50');
    await user.click(screen.getByRole('button', { name: 'Apply limits' }));
    await waitFor(() => {
      expect(api.calls(`/crawls/${JOB_ID}/link-graph`).at(-1)?.query.get('max_nodes')).toBe('50');
    });
  });

  it('can hide links back to shallower pages, and says how many there are', async () => {
    graphApi();
    const { user } = renderApp(`/graph?job=${JOB_ID}`);
    await user.click(await screen.findByRole('button', { name: 'Deeper only (2)' }));
    expect(screen.getByRole('button', { name: 'Deeper only (2)' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('figure', { name: /2 links shown/ })).toBeInTheDocument();
  });

  it('explains an empty graph and a failed one', async () => {
    graphApi(() => json(graph({ nodes: [], edges: [], total_nodes: 0, total_edges: 0 })));
    renderApp(`/graph?job=${JOB_ID}`);
    expect(await screen.findByText('No URL in this job yet')).toBeInTheDocument();
  });

  it('explains an analysis timeout', async () => {
    graphApi(() =>
      problem(503, 'analysis_timeout', {
        detail: 'The analysis took longer than 10 s and was stopped.',
      }),
    );
    renderApp(`/graph?job=${JOB_ID}`);
    expect(await screen.findByText('The analysis took too long')).toBeInTheDocument();
  });
});
