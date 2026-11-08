import { Network } from 'lucide-react';
import { useMemo, useState, type SubmitEvent } from 'react';
import { useSearchParams } from 'react-router';

import { describedBy } from '../../components/a11y';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { EmptyState, ErrorState, LoadingState, Notice } from '../../components/feedback';
import { Field, Input } from '../../components/Field';
import { cx } from '../../components/cx';
import type { LinkGraphResponse } from '../../lib/api/types';
import { formatNumber } from '../../lib/format';
import { useLinkGraph } from '../analysis/hooks';
import { JobGate } from '../crawls/JobGate';
import { GraphCanvas } from './GraphCanvas';
import { isDeeper, layoutGraph } from './layout';
import { GRAPH_LIMITS, parseLimit } from './limits';
import { GraphLegend, NodeDetails, NodeList } from './NodePanels';

export function LinkExplorerPage() {
  return <JobGate>{(jobId) => <LinkExplorer jobId={jobId} />}</JobGate>;
}

function LinkExplorer({ jobId }: { jobId: string }) {
  const [params, setParams] = useSearchParams();
  const maxNodes = parseLimit(params.get('nodes'), GRAPH_LIMITS.nodes);
  const maxEdges = parseLimit(params.get('edges'), GRAPH_LIMITS.edges);
  const graph = useLinkGraph(jobId, { max_nodes: maxNodes, max_edges: maxEdges });

  return (
    <div className="space-y-5">
      <LimitsForm
        key={`${String(maxNodes)}-${String(maxEdges)}`}
        maxNodes={maxNodes}
        maxEdges={maxEdges}
        onApply={(nodes, edges) => {
          setParams((current) => {
            const next = new URLSearchParams(current);
            next.set('nodes', String(nodes));
            next.set('edges', String(edges));
            return next;
          });
        }}
        loading={graph.isFetching}
      />
      {graph.isPending ? (
        <LoadingState label="Loading the link graph…" />
      ) : graph.isError ? (
        <ErrorState
          error={graph.error}
          onRetry={() => void graph.refetch()}
          retrying={graph.isFetching}
        />
      ) : graph.data.nodes.length === 0 ? (
        <EmptyState icon={<Network className="size-5" />} title="No URL in this job yet">
          The graph fills as the crawl admits URLs and stores pages.
        </EmptyState>
      ) : (
        <GraphView key={jobId} graph={graph.data} jobId={jobId} />
      )}
    </div>
  );
}

interface LimitsFormProps {
  maxNodes: number;
  maxEdges: number;
  onApply: (nodes: number, edges: number) => void;
  loading: boolean;
}

function LimitsForm({ maxNodes, maxEdges, onApply, loading }: LimitsFormProps) {
  const [nodes, setNodes] = useState(String(maxNodes));
  const [edges, setEdges] = useState(String(maxEdges));
  const [errors, setErrors] = useState<{ nodes?: string; edges?: string }>({});

  function check(
    raw: string,
    { min, max }: { min: number; max: number },
    name: string,
  ): string | undefined {
    if (!/^\d+$/.test(raw.trim())) return `Enter a whole number of ${name}.`;
    const value = Number(raw);
    return value < min || value > max
      ? `Between ${formatNumber(min)} and ${formatNumber(max)}.`
      : undefined;
  }

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = {
      nodes: check(nodes, GRAPH_LIMITS.nodes, 'nodes'),
      edges: check(edges, GRAPH_LIMITS.edges, 'links'),
    };
    setErrors(next);
    if (next.nodes !== undefined) document.getElementById('graph-nodes')?.focus();
    else if (next.edges !== undefined) document.getElementById('graph-edges')?.focus();
    else onApply(Number(nodes), Number(edges));
  }

  const nodesHint = `1 to ${formatNumber(GRAPH_LIMITS.nodes.max)}.`;
  const edgesHint = `0 to ${formatNumber(GRAPH_LIMITS.edges.max)}.`;
  return (
    <form
      noValidate
      onSubmit={onSubmit}
      className="flex flex-wrap items-start gap-3 rounded-xl border border-line bg-surface p-4"
    >
      <Field
        id="graph-nodes"
        label="Most nodes"
        hint={nodesHint}
        error={errors.nodes}
        className="w-36"
      >
        <Input
          id="graph-nodes"
          type="number"
          inputMode="numeric"
          min={GRAPH_LIMITS.nodes.min}
          max={GRAPH_LIMITS.nodes.max}
          value={nodes}
          onChange={(event) => {
            setNodes(event.target.value);
          }}
          aria-invalid={errors.nodes !== undefined || undefined}
          aria-describedby={describedBy('graph-nodes', nodesHint, errors.nodes)}
        />
      </Field>
      <Field
        id="graph-edges"
        label="Most links"
        hint={edgesHint}
        error={errors.edges}
        className="w-36"
      >
        <Input
          id="graph-edges"
          type="number"
          inputMode="numeric"
          min={GRAPH_LIMITS.edges.min}
          max={GRAPH_LIMITS.edges.max}
          value={edges}
          onChange={(event) => {
            setEdges(event.target.value);
          }}
          aria-invalid={errors.edges !== undefined || undefined}
          aria-describedby={describedBy('graph-edges', edgesHint, errors.edges)}
        />
      </Field>
      <Button type="submit" className="mt-[1.375rem]" busy={loading}>
        Apply limits
      </Button>
      <p className="basis-full text-xs text-ink-3 sm:basis-auto sm:self-center">
        Large graphs take longer to draw; the server cuts the graph at these limits in BFS order.
      </p>
    </form>
  );
}

function GraphView({ graph, jobId }: { graph: LinkGraphResponse; jobId: string }) {
  const layout = useMemo(() => layoutGraph(graph.nodes, graph.edges), [graph]);
  const [view, setView] = useState<'all' | 'deeper'>('all');
  const deeperEdges = useMemo(
    () => graph.edges.filter((edge) => isDeeper(layout, edge)),
    [graph, layout],
  );
  const edges = view === 'all' ? graph.edges : deeperEdges;
  const [selection, setSelection] = useState<{ graph: LinkGraphResponse; id: number } | null>(null);
  // Like the selection, a request to center a node belongs to one response: after a reload
  // (other limits, a refresh) the same index may be another node.
  const [focusRequest, setFocusRequest] = useState<{
    graph: LinkGraphResponse;
    id: number;
    seq: number;
  } | null>(null);
  const focus = focusRequest !== null && focusRequest.graph === graph ? focusRequest : null;
  // A selection belongs to one response: after a reload with other limits, it is cleared.
  const selectedId = selection !== null && selection.graph === graph ? selection.id : null;
  const selectedNode = selectedId === null ? undefined : graph.nodes[selectedId];

  const summary =
    `Internal link graph: ${formatNumber(graph.nodes.length)} nodes and ${formatNumber(edges.length)} links shown` +
    (graph.truncated
      ? `, cut from ${formatNumber(graph.total_nodes)} nodes and ${formatNumber(graph.total_edges)} links`
      : '');

  return (
    <div className="space-y-4">
      {graph.truncated ? (
        <Notice tone="warning" role="status" title="Partial graph: cut at the limits">
          Showing {formatNumber(graph.nodes.length)} of {formatNumber(graph.total_nodes)} nodes and{' '}
          {formatNumber(graph.edges.length)} of {formatNumber(graph.total_edges)} links (limits:{' '}
          {formatNumber(graph.max_nodes)} nodes, {formatNumber(graph.max_edges)} links). Nodes are
          cut in BFS order, so deeper URLs are missing; links to or from them are not drawn. Degrees
          still count every stored link. Raise the limits to see more.
        </Notice>
      ) : (
        <p className="text-sm text-ink-2">
          The whole graph: {formatNumber(graph.total_nodes)} nodes and{' '}
          {formatNumber(graph.total_edges)} stored internal links. Only links on pages the crawl
          stored are known.
        </p>
      )}
      <div className="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_22rem] xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <GraphLegend />
            <div
              role="group"
              aria-label="Links to draw"
              className="inline-flex rounded-md border border-line-strong p-0.5"
            >
              {(
                [
                  ['all', `All links (${formatNumber(graph.edges.length)})`],
                  ['deeper', `Deeper only (${formatNumber(deeperEdges.length)})`],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={view === key}
                  onClick={() => {
                    setView(key);
                  }}
                  className={cx(
                    'rounded px-2.5 py-1 text-xs font-medium transition-colors',
                    view === key ? 'bg-overlay text-ink' : 'text-ink-3 hover:text-ink',
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <GraphCanvas
            layout={layout}
            edges={edges}
            allEdges={graph.edges}
            selectedId={selectedId}
            onSelect={(id) => {
              setSelection(id === null ? null : { graph, id });
            }}
            focus={focus}
            label={summary}
          />
          <p className="text-xs text-ink-3">
            Columns are BFS depths; never-admitted link targets are last. Drag to pan, scroll or use
            the buttons to zoom. A selected node shows its outgoing links in blue and incoming links
            in orange. Redirects are not links.
          </p>
        </div>
        <div className="space-y-5">
          <Card title="Node details">
            {selectedNode === undefined ? (
              <p className="text-sm text-ink-3">Select a node in the graph or in the list below.</p>
            ) : (
              <NodeDetails
                node={selectedNode}
                linksHref={
                  selectedNode.page_id === null
                    ? null
                    : `/analysis?${new URLSearchParams({ job: jobId, tab: 'links', source: selectedNode.page_id }).toString()}`
                }
              />
            )}
          </Card>
          <Card title="Nodes">
            <NodeList
              nodes={graph.nodes}
              selectedId={selectedId}
              onSelect={(id) => {
                setSelection({ graph, id });
                setFocusRequest((current) => ({ graph, id, seq: (current?.seq ?? 0) + 1 }));
              }}
            />
          </Card>
        </div>
      </div>
    </div>
  );
}
