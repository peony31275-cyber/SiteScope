import { useState, type ReactNode } from 'react';
import { Link } from 'react-router';

import { ExternalUrl } from '../../components/ExternalUrl';
import { Input } from '../../components/Field';
import { cx } from '../../components/cx';
import type { GraphNodeItem } from '../../lib/api/types';
import { formatNumber, pathOf } from '../../lib/format';
import { KIND_DESCRIPTION, KIND_LABEL, LEGEND, nodeAppearance, type NodeShape } from './nodeStyle';

export function ShapeIcon({
  shape,
  color,
  size = 12,
}: {
  shape: NodeShape;
  color: string;
  size?: number;
}) {
  const r = size / 2 - 1.5;
  const c = size / 2;
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox={`0 0 ${String(size)} ${String(size)}`}
      className="shrink-0"
    >
      {shape === 'circle' && <circle cx={c} cy={c} r={r} fill={color} />}
      {shape === 'square' && (
        <rect x={1.5} y={1.5} width={size - 3} height={size - 3} rx={1.5} fill={color} />
      )}
      {shape === 'ring' && (
        <circle
          cx={c}
          cy={c}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
          strokeDasharray="2.5 2"
        />
      )}
    </svg>
  );
}

export function GraphLegend() {
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-ink-2">
      {LEGEND.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <ShapeIcon shape={item.shape} color={item.color} />
          {item.label}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <svg aria-hidden="true" width="18" height="8">
          <path d="M0,4 H18" stroke="#6b7380" strokeDasharray="3 3" />
        </svg>
        nofollow link
      </li>
    </ul>
  );
}

interface NodeDetailsProps {
  node: GraphNodeItem;
  /** The analysis page's links listing for this page's links. */
  linksHref: string | null;
}

/** What the API says about a node, and nothing more. */
export function NodeDetails({ node, linksHref }: NodeDetailsProps) {
  const look = nodeAppearance(node);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <ShapeIcon shape={look.shape} color={look.color} size={14} />
        <p className="text-sm font-medium text-ink">{KIND_LABEL[node.kind]}</p>
      </div>
      <p className="text-xs text-ink-3">{KIND_DESCRIPTION[node.kind]}</p>
      <dl className="grid gap-y-1.5 text-sm">
        <Row label="URL">
          <ExternalUrl url={node.url} className="font-mono text-xs" />
        </Row>
        <Row label="Title">
          {node.title ?? (
            <span className="text-ink-3">{node.kind === 'page' ? '(no title)' : 'not a page'}</span>
          )}
        </Row>
        <Row label="Crawl depth">
          {node.depth === null ? (
            <span className="text-ink-3">never admitted</span>
          ) : (
            formatNumber(node.depth)
          )}
        </Row>
        <Row label="Crawl state">
          {node.crawl_state ?? <span className="text-ink-3">never admitted</span>}
        </Row>
        <Row label="HTTP status">
          {node.http_status ?? <span className="text-ink-3">{look.meaning}</span>}
        </Row>
        <Row label="In-degree">{formatNumber(node.in_degree)}</Row>
        <Row label="Out-degree">
          {node.out_degree === null ? (
            <span className="text-ink-3">not a page: no links known</span>
          ) : (
            formatNumber(node.out_degree)
          )}
        </Row>
      </dl>
      {node.in_degree === 0 && (
        <p className="rounded-md border border-line bg-raised/50 p-2.5 text-xs text-ink-2">
          {node.depth === 0
            ? 'This is the seed URL: the crawl started here.'
            : 'No stored page links here. Only links on pages this crawl stored are known, so this does not mean that nothing links to it.'}
        </p>
      )}
      <p className="text-xs text-ink-3">
        Degrees count every stored internal link (one per linking page), also links beyond the
        graph&rsquo;s limits.
      </p>
      {linksHref !== null && (
        <Link to={linksHref} className="inline-block text-xs text-accent-ink hover:underline">
          Show this page&rsquo;s links in Site Analysis
        </Link>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2 border-b border-line/50 py-1">
      <dt className="text-ink-3">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  );
}

interface NodeListProps {
  nodes: readonly GraphNodeItem[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

/** Every node of the graph as a keyboard-reachable list, filterable by URL: the accessible way
 * to explore the graph (selecting here selects and centers the node in the drawing). */
export function NodeList({ nodes, selectedId, onSelect }: NodeListProps) {
  const [filter, setFilter] = useState('');
  const needle = filter.trim().toLowerCase();
  const shown =
    needle === '' ? nodes : nodes.filter((node) => node.url.toLowerCase().includes(needle));
  return (
    <div className="space-y-2">
      <label htmlFor="node-filter" className="block text-xs font-medium text-ink-2">
        Find a node
      </label>
      <Input
        id="node-filter"
        type="search"
        value={filter}
        placeholder="Part of a URL"
        onChange={(event) => {
          setFilter(event.target.value);
        }}
      />
      <p className="text-xs text-ink-3" aria-live="polite">
        {formatNumber(shown.length)} of {formatNumber(nodes.length)} nodes
      </p>
      <ul className="max-h-72 space-y-0.5 overflow-y-auto pr-1" aria-label="Graph nodes">
        {shown.map((node) => {
          const look = nodeAppearance(node);
          const active = node.id === selectedId;
          return (
            <li key={node.id}>
              <button
                type="button"
                onClick={() => {
                  onSelect(node.id);
                }}
                aria-pressed={active}
                className={cx(
                  'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs transition-colors',
                  active ? 'bg-overlay text-ink' : 'text-ink-2 hover:bg-raised hover:text-ink',
                )}
              >
                <ShapeIcon shape={look.shape} color={look.color} />
                <span className="min-w-0 flex-1 truncate font-mono" title={node.url}>
                  {pathOf(node.url)}
                </span>
                <span className="shrink-0 text-ink-3 tabular-nums" title="In-degree">
                  ←{node.in_degree}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
