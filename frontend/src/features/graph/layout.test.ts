import { describe, expect, it } from 'vitest';

import { graph, node } from '../../tests/fixtures';
import { COLUMN_GAP, edgePath, isDeeper, layoutGraph } from './layout';
import { nodeAppearance } from './nodeStyle';

describe('layoutGraph', () => {
  it('puts each BFS depth in its own column, and never-admitted targets last', () => {
    const { nodes, edges } = graph();
    const layout = layoutGraph(nodes, edges);
    expect(layout.columns.map((column) => [column.label, column.count])).toEqual([
      ['Depth 0', 1],
      ['Depth 1', 2],
      ['Never admitted', 1],
    ]);
    expect(layout.nodes.map((item) => item.x)).toEqual([0, COLUMN_GAP, COLUMN_GAP, 2 * COLUMN_GAP]);
    expect(layout.nodes[3]?.column).toBe(2);
  });

  it('keeps nodes in their API order: nodes[i] is the node with id i', () => {
    const { nodes, edges } = graph();
    const layout = layoutGraph(nodes, edges);
    expect(layout.nodes.map((item) => item.node.id)).toEqual([0, 1, 2, 3]);
  });

  it('orders a column by the position of the nodes linking to it', () => {
    // Depth 1: a (linked from z, bottom of depth 0) and b (linked from y, top).
    const nodes = [
      node({ id: 0, url: 'http://x.test/y', depth: 0 }),
      node({ id: 1, url: 'http://x.test/z', depth: 0 }),
      node({ id: 2, url: 'http://x.test/a', depth: 1 }),
      node({ id: 3, url: 'http://x.test/b', depth: 1 }),
    ];
    const edges = [
      { source: 1, target: 2, nofollow: false },
      { source: 0, target: 3, nofollow: false },
    ];
    const layout = layoutGraph(nodes, edges);
    expect(layout.nodes[3]?.y).toBeLessThan(layout.nodes[2]?.y ?? 0);
  });

  it('is deterministic', () => {
    const { nodes, edges } = graph();
    expect(layoutGraph(nodes, edges)).toEqual(layoutGraph(nodes, edges));
  });

  it('handles an empty graph', () => {
    expect(layoutGraph([], [])).toEqual({
      nodes: [],
      columns: [],
      bounds: { minX: 0, minY: 0, maxX: 0, maxY: 0 },
    });
  });

  it('tells deeper links from links back to shallower pages', () => {
    const { nodes, edges } = graph();
    const layout = layoutGraph(nodes, edges);
    expect(edges.map((edge) => isDeeper(layout, edge))).toEqual([true, true, false]);
    const [home, about] = [layout.nodes[0], layout.nodes[1]];
    if (home === undefined || about === undefined) throw new Error('missing nodes');
    expect(edgePath(home, about)).toMatch(/^M\S+ C/);
    expect(edgePath(about, home)).toMatch(/^M\S+ C/);
  });
});

describe('nodeAppearance', () => {
  it('gives each kind its own shape, and a crawled URL the color of its own response', () => {
    const [page, missing, target] = [graph().nodes[0], graph().nodes[2], graph().nodes[3]];
    if (page === undefined || missing === undefined || target === undefined)
      throw new Error('missing nodes');
    expect(nodeAppearance(page).shape).toBe('circle');
    expect(nodeAppearance(missing)).toMatchObject({ shape: 'square', meaning: 'Missing (404)' });
    expect(nodeAppearance(target)).toMatchObject({ shape: 'ring', meaning: 'Never requested' });
    expect(
      nodeAppearance(
        node({ id: 9, url: 'http://x.test/r', kind: 'crawl_url', page_id: null, http_status: 301 }),
      ).meaning,
    ).toBe('Redirect (301)');
    expect(
      nodeAppearance(
        node({
          id: 9,
          url: 'http://x.test/p',
          kind: 'crawl_url',
          page_id: null,
          crawl_state: 'pending',
          http_status: null,
        }),
      ).meaning,
    ).toBe('Not crawled yet');
  });
});
