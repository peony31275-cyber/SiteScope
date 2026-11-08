/**
 * The link graph, drawn in SVG with pan and zoom.
 *
 * Rendering stays cheap at the API's limits: the layout is computed once per graph; the edge
 * and node layers are memoized and do not re-render on pan, zoom or selection (pan and zoom
 * set one `transform` attribute directly); a selection draws its own small layer on top. Above
 * ARROWS_UP_TO edges, the edges are drawn as two combined paths instead of one element each.
 * Listeners and the resize observer are removed on unmount.
 *
 * The drawing is presentational for assistive technology (a figure with a summary as its name):
 * the node list beside it reaches every node with the keyboard, and selecting there selects
 * and centers the node here; the zoom buttons are keyboard buttons.
 */
import { Maximize2, Minus, Plus } from 'lucide-react';
import {
  memo,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type PointerEvent,
  type ReactNode,
} from 'react';

import type { GraphEdgeItem } from '../../lib/api/types';
import { pathOf } from '../../lib/format';
import { edgePath, type GraphLayout, type PositionedNode } from './layout';
import { nodeAppearance } from './nodeStyle';

// Low enough to fit the tallest graph the API returns (2000 nodes in one column, 30 px apart).
const MIN_SCALE = 0.005;
const MAX_SCALE = 4;
const LABEL_SCALE = 0.6;
/** Up to this many edges, each is its own path with an arrowhead. */
const ARROWS_UP_TO = 1500;
const SURFACE = '#0d0e10';
const OUTGOING = '#3987e5';
const INCOMING = '#d95926';

interface Transform {
  x: number;
  y: number;
  k: number;
}

function clampScale(value: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
}

function shortLabel(url: string): string {
  const path = pathOf(url);
  return path.length > 36 ? `${path.slice(0, 35)}…` : path;
}

interface GraphCanvasProps {
  layout: GraphLayout;
  /** The edges to draw (the view may hide links to shallower depths). */
  edges: readonly GraphEdgeItem[];
  /** Every edge of the response: a selection shows all of its node's links. */
  allEdges: readonly GraphEdgeItem[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  /** Center the view on this node whenever `seq` changes (a selection from the list). */
  focus: { id: number; seq: number } | null;
  label: string;
}

export function GraphCanvas({
  layout,
  edges,
  allEdges,
  selectedId,
  onSelect,
  focus,
  label,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<SVGGElement>(null);
  const transform = useRef<Transform>({ x: 0, y: 0, k: 1 });
  const fitted = useRef(false);
  const frame = useRef(0);
  const drag = useRef<{
    pointerId: number;
    x: number;
    y: number;
    origin: Transform;
    moved: boolean;
    node: number | null;
  } | null>(null);
  const [showLabels, setShowLabels] = useState(false);
  const rawId = useId();
  const id = rawId.replace(/[^a-zA-Z0-9_-]/g, '');

  const apply = useCallback((next: Transform) => {
    transform.current = next;
    viewportRef.current?.setAttribute(
      'transform',
      `translate(${String(next.x)} ${String(next.y)}) scale(${String(next.k)})`,
    );
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      setShowLabels(next.k >= LABEL_SCALE);
    });
  }, []);

  const fit = useCallback(() => {
    const container = containerRef.current;
    if (container === null) return;
    const { width, height } = container.getBoundingClientRect();
    if (width === 0 || height === 0) return;
    const { minX, minY, maxX, maxY } = layout.bounds;
    const k = clampScale(
      Math.min(width / Math.max(1, maxX - minX), height / Math.max(1, maxY - minY), 1.4),
    );
    apply({
      k,
      x: (width - (maxX - minX) * k) / 2 - minX * k,
      y: (height - (maxY - minY) * k) / 2 - minY * k,
    });
    fitted.current = true;
  }, [layout, apply]);

  const zoomBy = useCallback(
    (factor: number, at?: { x: number; y: number }) => {
      const container = containerRef.current;
      if (container === null) return;
      const rect = container.getBoundingClientRect();
      const center = at ?? { x: rect.width / 2, y: rect.height / 2 };
      const current = transform.current;
      const k = clampScale(current.k * factor);
      const ratio = k / current.k;
      apply({
        k,
        x: center.x - (center.x - current.x) * ratio,
        y: center.y - (center.y - current.y) * ratio,
      });
    },
    [apply],
  );

  // A new graph is fitted to the view; a view that had no size yet is fitted once it has one.
  useEffect(() => {
    fitted.current = false;
    fit();
  }, [fit]);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return undefined;
    const observer = new ResizeObserver(() => {
      if (!fitted.current) fit();
    });
    observer.observe(container);
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = container.getBoundingClientRect();
      zoomBy(Math.exp(-event.deltaY * 0.0015), {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    };
    container.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      observer.disconnect();
      container.removeEventListener('wheel', onWheel);
      cancelAnimationFrame(frame.current);
    };
  }, [fit, zoomBy]);

  useEffect(() => {
    if (focus === null) return;
    const node = layout.nodes[focus.id];
    const container = containerRef.current;
    if (node === undefined || container === null) return;
    const { width, height } = container.getBoundingClientRect();
    const k = Math.max(transform.current.k, 0.9);
    apply({ k, x: width / 2 - node.x * k, y: height / 2 - node.y * k });
  }, [focus, layout, apply]);

  function onPointerDown(event: PointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    const target = event.target instanceof Element ? event.target.closest('[data-node]') : null;
    const node = target?.getAttribute('data-node');
    drag.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      origin: transform.current,
      moved: false,
      node: node === undefined || node === null ? null : Number(node),
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: PointerEvent<SVGSVGElement>) {
    const state = drag.current;
    if (state?.pointerId !== event.pointerId) return;
    const dx = event.clientX - state.x;
    const dy = event.clientY - state.y;
    if (!state.moved && Math.hypot(dx, dy) < 4) return;
    state.moved = true;
    apply({ ...state.origin, x: state.origin.x + dx, y: state.origin.y + dy });
  }

  function onPointerUp(event: PointerEvent<SVGSVGElement>) {
    const state = drag.current;
    drag.current = null;
    if (state?.pointerId !== event.pointerId || state.moved) return;
    onSelect(state.node); // a click: the node under the pointer, or none (clears)
  }

  const selected = selectedId === null ? undefined : layout.nodes[selectedId];

  return (
    <figure
      ref={containerRef}
      aria-label={label}
      className="relative m-0 h-[min(68vh,640px)] min-h-[420px] overflow-hidden rounded-lg border border-line bg-canvas"
    >
      <svg
        aria-hidden="true"
        className="size-full cursor-grab touch-none select-none active:cursor-grabbing"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          drag.current = null;
        }}
      >
        <defs>
          <Arrow id={`${id}-muted`} color="#6b7380" />
          <Arrow id={`${id}-out`} color={OUTGOING} />
          <Arrow id={`${id}-in`} color={INCOMING} />
        </defs>
        <g ref={viewportRef}>
          <ColumnLabels layout={layout} />
          <g opacity={selected === undefined ? 1 : 0.3}>
            <EdgesLayer layout={layout} edges={edges} marker={`url(#${id}-muted)`} />
            <NodesLayer layout={layout} showLabels={showLabels || layout.nodes.length <= 40} />
          </g>
          {selected !== undefined && (
            <SelectionLayer selected={selected} layout={layout} edges={allEdges} markerId={id} />
          )}
        </g>
      </svg>
      <div className="absolute top-2 right-2 flex flex-col gap-1">
        <CanvasButton
          label="Zoom in"
          onClick={() => {
            zoomBy(1.25);
          }}
        >
          <Plus aria-hidden="true" className="size-4" />
        </CanvasButton>
        <CanvasButton
          label="Zoom out"
          onClick={() => {
            zoomBy(0.8);
          }}
        >
          <Minus aria-hidden="true" className="size-4" />
        </CanvasButton>
        <CanvasButton label="Fit the whole graph" onClick={fit}>
          <Maximize2 aria-hidden="true" className="size-4" />
        </CanvasButton>
      </div>
    </figure>
  );
}

function CanvasButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="flex size-8 items-center justify-center rounded-md border border-line-strong bg-surface/90 text-ink-2 hover:bg-raised hover:text-ink"
    >
      {children}
    </button>
  );
}

function Arrow({ id, color }: { id: string; color: string }) {
  return (
    <marker
      id={id}
      viewBox="0 0 10 10"
      refX="9"
      refY="5"
      markerWidth="7"
      markerHeight="7"
      orient="auto-start-reverse"
      markerUnits="userSpaceOnUse"
    >
      <path d="M0,1 L9,5 L0,9 z" fill={color} />
    </marker>
  );
}

const ColumnLabels = memo(function ColumnLabels({ layout }: { layout: GraphLayout }) {
  return (
    <g>
      {layout.columns.map((column) => (
        <text
          key={column.label}
          x={column.x}
          y={column.top}
          textAnchor="middle"
          fontSize={12}
          fill="#8a919c"
        >
          {column.label} ({column.count})
        </text>
      ))}
    </g>
  );
});

const EdgesLayer = memo(function EdgesLayer({
  layout,
  edges,
  marker,
}: {
  layout: GraphLayout;
  edges: readonly GraphEdgeItem[];
  marker: string;
}) {
  if (edges.length > ARROWS_UP_TO) {
    let deeper = '';
    let other = '';
    for (const edge of edges) {
      const source = layout.nodes[edge.source];
      const target = layout.nodes[edge.target];
      if (source === undefined || target === undefined) continue;
      if (target.column > source.column) deeper += edgePath(source, target);
      else other += edgePath(source, target);
    }
    return (
      <g fill="none" strokeWidth={1}>
        <path d={deeper} stroke="#6b7380" strokeOpacity={0.35} />
        <path d={other} stroke="#6b7380" strokeOpacity={0.18} />
      </g>
    );
  }
  return (
    <g fill="none">
      {edges.map((edge) => {
        const source = layout.nodes[edge.source];
        const target = layout.nodes[edge.target];
        if (source === undefined || target === undefined) return null;
        const deeper = target.column > source.column;
        return (
          <path
            key={`${String(edge.source)}-${String(edge.target)}`}
            d={edgePath(source, target)}
            stroke="#6b7380"
            strokeOpacity={deeper ? 0.6 : 0.35}
            strokeWidth={1.1}
            strokeDasharray={edge.nofollow ? '3 3' : undefined}
            markerEnd={marker}
          />
        );
      })}
    </g>
  );
});

const NodesLayer = memo(function NodesLayer({
  layout,
  showLabels,
}: {
  layout: GraphLayout;
  showLabels: boolean;
}) {
  return (
    <g>
      {layout.nodes.map((item) => (
        <NodeMark key={item.node.id} item={item} showLabel={showLabels} />
      ))}
    </g>
  );
});

function NodeMark({ item, showLabel }: { item: PositionedNode; showLabel: boolean }) {
  const look = nodeAppearance(item.node);
  const { x, y, radius } = item;
  return (
    <g data-node={item.node.id} className="cursor-pointer">
      <circle cx={x} cy={y} r={Math.max(12, radius + 6)} fill="transparent" />
      {look.shape === 'circle' && (
        <circle cx={x} cy={y} r={radius} fill={look.color} stroke={SURFACE} strokeWidth={2} />
      )}
      {look.shape === 'square' && (
        <rect
          x={x - radius}
          y={y - radius}
          width={radius * 2}
          height={radius * 2}
          rx={1.5}
          fill={look.color}
          stroke={SURFACE}
          strokeWidth={2}
        />
      )}
      {look.shape === 'ring' && (
        <circle
          cx={x}
          cy={y}
          r={radius}
          fill={SURFACE}
          stroke={look.color}
          strokeWidth={1.5}
          strokeDasharray="2.5 2"
        />
      )}
      {showLabel && (
        <text x={x + radius + 6} y={y + 4} fontSize={11} fill="#aab0ba">
          {shortLabel(item.node.url)}
        </text>
      )}
    </g>
  );
}

function SelectionLayer({
  selected,
  layout,
  edges,
  markerId,
}: {
  selected: PositionedNode;
  layout: GraphLayout;
  edges: readonly GraphEdgeItem[];
  markerId: string;
}) {
  const id = selected.node.id;
  const neighbors = new Set<number>([id]);
  const paths: { key: string; d: string; color: string; marker: string }[] = [];
  for (const edge of edges) {
    if (edge.source !== id && edge.target !== id) continue;
    const source = layout.nodes[edge.source];
    const target = layout.nodes[edge.target];
    if (source === undefined || target === undefined) continue;
    const outgoing = edge.source === id;
    neighbors.add(outgoing ? edge.target : edge.source);
    paths.push({
      key: `${String(edge.source)}-${String(edge.target)}`,
      d: edgePath(source, target),
      color: outgoing ? OUTGOING : INCOMING,
      marker: `url(#${markerId}-${outgoing ? 'out' : 'in'})`,
    });
  }
  return (
    <g>
      <g fill="none" strokeWidth={1.75}>
        {paths.map((path) => (
          <path key={path.key} d={path.d} stroke={path.color} markerEnd={path.marker} />
        ))}
      </g>
      {[...neighbors].map((neighbor) => {
        const item = layout.nodes[neighbor];
        return item === undefined ? null : <NodeMark key={neighbor} item={item} showLabel />;
      })}
      <circle
        cx={selected.x}
        cy={selected.y}
        r={selected.radius + 5}
        fill="none"
        stroke="#6da7ec"
        strokeWidth={2}
      />
    </g>
  );
}
