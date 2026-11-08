/**
 * A layered layout of the link graph, by BFS depth: one column per depth, left to right, and a
 * last column for link targets the job never admitted (they have no depth). Within a column,
 * nodes are ordered by the mean position of the nodes linking to them from earlier columns
 * (one barycenter pass), which keeps most links short and uncrossed.
 *
 * Cost: O(nodes + edges) plus one sort per column, so a graph at the API's limits (2,000 nodes,
 * 10,000 edges) lays out in milliseconds. No force simulation: the layout is deterministic, the
 * same graph always looks the same, and nothing keeps running after it is drawn.
 */
import type { GraphEdgeItem, GraphNodeItem } from '../../lib/api/types';

export const COLUMN_GAP = 280;
export const ROW_GAP = 30;
const NEVER_ADMITTED = -1;

export interface PositionedNode {
  node: GraphNodeItem;
  x: number;
  y: number;
  column: number;
  radius: number;
}

export interface GraphColumn {
  label: string;
  x: number;
  top: number;
  count: number;
}

export interface GraphLayout {
  /** In the API's order: `nodes[i]` is the node with `id` i. */
  nodes: PositionedNode[];
  columns: GraphColumn[];
  bounds: { minX: number; minY: number; maxX: number; maxY: number };
}

export function nodeRadius(node: GraphNodeItem): number {
  if (node.kind !== 'page') return 4.5;
  return 5 + Math.min(5, Math.sqrt(node.in_degree));
}

export function layoutGraph(
  nodes: readonly GraphNodeItem[],
  edges: readonly GraphEdgeItem[],
): GraphLayout {
  const depthKeys = [...new Set(nodes.map((node) => node.depth ?? NEVER_ADMITTED))].sort((a, b) => {
    if (a === NEVER_ADMITTED) return 1;
    if (b === NEVER_ADMITTED) return -1;
    return a - b;
  });
  const columnOf = new Map(depthKeys.map((key, index) => [key, index]));
  const members: number[][] = depthKeys.map(() => []);
  const column = nodes.map((node) => columnOf.get(node.depth ?? NEVER_ADMITTED) ?? 0);
  nodes.forEach((node, index) => {
    members[column[index] ?? 0]?.push(node.id);
  });

  // Links from earlier columns, per target.
  const sources = new Map<number, number[]>();
  for (const edge of edges) {
    const from = column[edge.source];
    const to = column[edge.target];
    if (from === undefined || to === undefined || from >= to) continue;
    const list = sources.get(edge.target);
    if (list === undefined) sources.set(edge.target, [edge.source]);
    else list.push(edge.source);
  }

  // Normalized position (0..1) of each node within its column, column by column.
  const position = new Map<number, number>();
  members.forEach((ids, index) => {
    if (index > 0) {
      const keyed = ids.map((id, original) => {
        const linked = (sources.get(id) ?? [])
          .map((source) => position.get(source))
          .filter((value) => value !== undefined);
        const fallback = ids.length > 1 ? original / (ids.length - 1) : 0.5;
        const center =
          linked.length > 0
            ? linked.reduce((sum, value) => sum + value, 0) / linked.length
            : fallback;
        return { id, center, original };
      });
      keyed.sort((a, b) => a.center - b.center || a.original - b.original);
      ids.splice(0, ids.length, ...keyed.map((item) => item.id));
    }
    ids.forEach((id, row) => {
      position.set(id, ids.length > 1 ? row / (ids.length - 1) : 0.5);
    });
  });

  const placed = new Map<number, { x: number; y: number }>();
  members.forEach((ids, index) => {
    const offset = ((ids.length - 1) * ROW_GAP) / 2;
    ids.forEach((id, row) => {
      placed.set(id, { x: index * COLUMN_GAP, y: row * ROW_GAP - offset });
    });
  });

  const positioned = nodes.map((node, index) => {
    const point = placed.get(node.id) ?? { x: 0, y: 0 };
    return { node, x: point.x, y: point.y, column: column[index] ?? 0, radius: nodeRadius(node) };
  });

  const tallest = Math.max(1, ...members.map((ids) => ids.length));
  const top = -((tallest - 1) * ROW_GAP) / 2 - 40;
  const columns = depthKeys.map((key, index) => ({
    label: key === NEVER_ADMITTED ? 'Never admitted' : `Depth ${String(key)}`,
    x: index * COLUMN_GAP,
    top,
    count: members[index]?.length ?? 0,
  }));

  const xs = positioned.map((item) => item.x);
  const ys = positioned.map((item) => item.y);
  const bounds =
    positioned.length === 0
      ? { minX: 0, minY: 0, maxX: 0, maxY: 0 }
      : {
          minX: Math.min(...xs) - 40,
          minY: Math.min(top, ...ys) - 20,
          maxX: Math.max(...xs) + 220,
          maxY: Math.max(...ys) + 30,
        };
  return { nodes: positioned, columns, bounds };
}

/** Whether a link goes to a deeper column (the BFS direction). Links to the same or a
 * shallower depth (navigation back to the home page, siblings) are drawn as arcs. */
export function isDeeper(layout: GraphLayout, edge: GraphEdgeItem): boolean {
  const source = layout.nodes[edge.source];
  const target = layout.nodes[edge.target];
  return source !== undefined && target !== undefined && target.column > source.column;
}

/** The SVG path of a link, ending just before the target's edge (room for the arrowhead). */
export function edgePath(source: PositionedNode, target: PositionedNode): string {
  if (source === target) {
    const { x, y, radius } = source;
    return `M${String(x + radius)},${String(y)} C${String(x + 26)},${String(y - 26)} ${String(x - 26)},${String(y - 26)} ${String(x - radius - 2)},${String(y - 2)}`;
  }
  const endX = target.x - target.radius - 3;
  if (target.x > source.x) {
    const startX = source.x + source.radius;
    const middle = (startX + endX) / 2;
    return `M${String(startX)},${String(source.y)} C${String(middle)},${String(source.y)} ${String(middle)},${String(target.y)} ${String(endX)},${String(target.y)}`;
  }
  // To the same depth or a shallower one: bulge to the left of both nodes.
  const bulge =
    Math.min(source.x, target.x) - 50 - Math.min(180, Math.abs(target.y - source.y) * 0.3);
  const startX = source.x - source.radius;
  return `M${String(startX)},${String(source.y)} C${String(bulge)},${String(source.y)} ${String(bulge)},${String(target.y)} ${String(endX)},${String(target.y)}`;
}
