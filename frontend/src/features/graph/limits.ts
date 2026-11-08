/** The API's bounds (`max_nodes` 1-2000, `max_edges` 0-10000) and the dashboard's defaults,
 * kept below the API's own (500 and 2000) so a first view stays light on an ordinary laptop. */
export const GRAPH_LIMITS = {
  nodes: { min: 1, max: 2000, initial: 300 },
  edges: { min: 0, max: 10_000, initial: 1500 },
} as const;

export function parseLimit(
  raw: string | null,
  { min, max, initial }: { min: number; max: number; initial: number },
): number {
  if (raw === null || !/^\d{1,5}$/.test(raw)) return initial;
  const value = Number(raw);
  return value >= min && value <= max ? value : initial;
}
