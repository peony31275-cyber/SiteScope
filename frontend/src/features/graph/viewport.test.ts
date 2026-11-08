import { describe, expect, it } from 'vitest';

import { node } from '../../tests/fixtures';
import type { GraphLayout } from './layout';
import { covers, grown, LABEL_WIDTH, nodesForLabels, visibleRegion } from './viewport';

/** One column of `count` nodes, 30 units apart, at x = 0 (and a second column at x = 280). */
function column(count: number): GraphLayout {
  const nodes = Array.from({ length: count }, (_, id) => ({
    node: node({ id, url: `http://x.test/p${String(id)}`, depth: id % 2 }),
    x: (id % 2) * 280,
    y: Math.floor(id / 2) * 30,
    column: id % 2,
    radius: 5,
  }));
  return { nodes, columns: [], bounds: { minX: -40, minY: 0, maxX: 500, maxY: count * 15 } };
}

describe('the labelled part of the graph', () => {
  it('is the layout region the view shows', () => {
    // Translated by (100, 50) on screen, then scaled by 2: a 400 x 300 view.
    expect(visibleRegion({ x: 100, y: 50, k: 2 }, 400, 300)).toEqual({
      minX: -50,
      minY: -25,
      maxX: 150,
      maxY: 125,
    });
  });

  it('grows the region by a share of its size, and knows what it covers', () => {
    const view = { minX: 0, minY: 0, maxX: 100, maxY: 50 };
    const region = grown(view, 0.5);
    expect(region).toEqual({ minX: -50, minY: -25, maxX: 150, maxY: 75 });
    expect(covers(region, view)).toBe(true);
    expect(covers(view, region)).toBe(false);
    expect(covers(region, { ...view, maxX: 151 })).toBe(false);
  });

  it('labels the nodes in the region, and those whose label reaches into it', () => {
    const layout = column(2000);
    // Rows 10 to 20 of the first column only (x from -10 to 100).
    const picked = nodesForLabels(layout, { minX: -10, minY: 300, maxX: 100, maxY: 600 });
    expect(picked.length).toBeGreaterThan(0);
    expect(picked.length).toBeLessThan(40); // not 2,000
    expect(picked.every((item) => item.y >= 295 && item.y <= 605)).toBe(true);
    expect(picked.some((item) => item.column === 1)).toBe(false); // x = 280: right of the region
    // A region right of the first column still labels it: its labels reach into the region.
    const right = nodesForLabels(layout, { minX: 200, minY: 300, maxX: 260, maxY: 330 });
    expect(right.map((item) => item.x)).toContain(0);
    const beyond = nodesForLabels(layout, {
      minX: 5 + LABEL_WIDTH + 1,
      minY: 300,
      maxX: 270,
      maxY: 330,
    });
    expect(beyond.map((item) => item.x)).not.toContain(0);
  });

  it('labels nothing outside the graph', () => {
    expect(nodesForLabels(column(100), { minX: 0, minY: 5000, maxX: 100, maxY: 6000 })).toEqual([]);
  });
});
