/**
 * How a node looks. The shape carries the kind (so color is never the only signal): a circle
 * is a stored page, a square an admitted URL without a page, a dashed ring a link target the
 * job never admitted (never requested). A square's color is the state of its own response.
 */
import type { GraphNodeItem, NodeKind } from '../../lib/api/types';

export type NodeShape = 'circle' | 'square' | 'ring';

export interface NodeAppearance {
  shape: NodeShape;
  color: string;
  /** What the color means, in words (the details panel and the legend). */
  meaning: string;
}

export const KIND_LABEL: Record<NodeKind, string> = {
  page: 'Stored page',
  crawl_url: 'Admitted URL without a page',
  link_target: 'Never admitted',
};

export const KIND_DESCRIPTION: Record<NodeKind, string> = {
  page: 'A fetched 2xx HTML page the crawl stored.',
  crawl_url:
    'A URL the job admitted that has no stored page: not crawled yet, a redirect, an HTTP error, another content type, or skipped.',
  link_target:
    'Only known as the target of internal links: the job never admitted it, so it was never requested.',
};

const PAGE = '#3987e5';
const NEUTRAL = '#646b75';
const MUTED = '#8a919c';

export function nodeAppearance(node: GraphNodeItem): NodeAppearance {
  if (node.kind === 'page') return { shape: 'circle', color: PAGE, meaning: 'Stored HTML page' };
  if (node.kind === 'link_target')
    return { shape: 'ring', color: MUTED, meaning: 'Never requested' };
  const status = node.http_status;
  switch (node.crawl_state) {
    case 'pending':
    case 'queued':
    case 'in_progress':
      return { shape: 'square', color: NEUTRAL, meaning: 'Not crawled yet' };
    case 'skipped':
      return {
        shape: 'square',
        color: NEUTRAL,
        meaning: 'Skipped: never requested (robots.txt or policy)',
      };
    case 'cancelled':
      return { shape: 'square', color: NEUTRAL, meaning: 'Cancelled before it was crawled' };
    default:
      break;
  }
  if (status === null) return { shape: 'square', color: NEUTRAL, meaning: 'No response' };
  if (status >= 300 && status < 400)
    return { shape: 'square', color: '#fab219', meaning: `Redirect (${String(status)})` };
  if (status === 404 || status === 410)
    return { shape: 'square', color: '#d03b3b', meaning: `Missing (${String(status)})` };
  if (status >= 200 && status < 300)
    return {
      shape: 'square',
      color: MUTED,
      meaning: `Fetched (${String(status)}), not stored as a page`,
    };
  return { shape: 'square', color: '#ec835a', meaning: `HTTP error (${String(status)})` };
}

export const LEGEND: readonly { shape: NodeShape; color: string; label: string }[] = [
  { shape: 'circle', color: PAGE, label: 'Stored page' },
  { shape: 'square', color: '#fab219', label: 'Redirect' },
  { shape: 'square', color: '#d03b3b', label: 'Missing (404, 410)' },
  { shape: 'square', color: '#ec835a', label: 'Other HTTP error' },
  { shape: 'square', color: MUTED, label: 'Fetched, not a page' },
  { shape: 'square', color: NEUTRAL, label: 'Not requested or no response' },
  { shape: 'ring', color: MUTED, label: 'Never admitted' },
];
