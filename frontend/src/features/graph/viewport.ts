/**
 * Which nodes get a label (Phase 10). At the API's limits a graph has 2,000 nodes; drawing a
 * label for each when the zoom crosses the label scale cost a 54 to 72 ms frame, and frames
 * stayed slower while every label was drawn (benchmarks/dashboard_graph.py). Only the nodes in
 * and around the view get one: the region is the view grown by half its size on every side, and
 * it is computed again only when the view leaves it.
 */
import type { GraphLayout, PositionedNode } from './layout';

/** A rectangle in layout coordinates. */
export interface Region {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** The view's transform: translate by (x, y), then scale by k. */
export interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

/** A label starts right of its node; up to 36 characters at 11 px fit in this width. */
export const LABEL_WIDTH = 240;
/** How far around the view labels are kept, as a share of the view's width and height. */
export const LABEL_MARGIN = 0.5;

/** The part of the layout that a `width` x `height` view shows under `transform`. */
export function visibleRegion(transform: ViewTransform, width: number, height: number): Region {
  return {
    minX: -transform.x / transform.k,
    minY: -transform.y / transform.k,
    maxX: (width - transform.x) / transform.k,
    maxY: (height - transform.y) / transform.k,
  };
}

/** `region` grown by `share` of its width and height on every side. */
export function grown(region: Region, share: number): Region {
  const dx = (region.maxX - region.minX) * share;
  const dy = (region.maxY - region.minY) * share;
  return {
    minX: region.minX - dx,
    minY: region.minY - dy,
    maxX: region.maxX + dx,
    maxY: region.maxY + dy,
  };
}

/** Whether `outer` contains all of `inner`. */
export function covers(outer: Region, inner: Region): boolean {
  return (
    outer.minX <= inner.minX &&
    outer.minY <= inner.minY &&
    outer.maxX >= inner.maxX &&
    outer.maxY >= inner.maxY
  );
}

/** The nodes whose label can show in `region`: those inside it, and those far enough left of it
 * for their label to reach into it. */
export function nodesForLabels(layout: GraphLayout, region: Region): PositionedNode[] {
  return layout.nodes.filter(
    (item) =>
      item.x + item.radius + LABEL_WIDTH >= region.minX &&
      item.x - item.radius <= region.maxX &&
      item.y + item.radius >= region.minY &&
      item.y - item.radius <= region.maxY,
  );
}
