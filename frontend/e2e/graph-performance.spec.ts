/**
 * Link graph rendering at increasing limits, against the real API (Phase 10). Opt-in:
 * `benchmarks/dashboard_graph.py` runs it with E2E_GRAPH_PERF=1 on a disposable database that
 * holds one large crawl job (E2E_GRAPH_JOB), and collects the measurements written to
 * E2E_GRAPH_OUTPUT.
 *
 * Each load is its own test, so each starts in a fresh browser context (a first visit). Measured:
 * the link-graph response (network timing), the time from navigation until every node is drawn,
 * long tasks (over 50 ms) until then, the JS heap and the DOM size; then a scripted pan (40
 * pointer moves) and zoom (25 wheel steps in, which crosses the scale where labels appear on the
 * largest graphs), with the intervals between animation frames meanwhile. Headless Edge may
 * rasterize in software: compare the sizes with each other, not with a desktop browser's.
 */
import { writeFileSync } from 'node:fs';

import { test, type Page } from '@playwright/test';

interface Probe {
  longTasks: number[];
  frames: number[];
  recording: boolean;
}
type ProbeGlobal = typeof globalThis & { __probe?: Probe };
type MemoryPerformance = Performance & { memory?: { usedJSHeapSize: number } };

const ENABLED = process.env.E2E_GRAPH_PERF === '1';
const JOB = process.env.E2E_GRAPH_JOB ?? '';
const OUTPUT = process.env.E2E_GRAPH_OUTPUT ?? '';
const REPEAT = Number(process.env.E2E_GRAPH_REPEAT ?? '3');
const SIZES: readonly (readonly [number, number])[] = [
  [300, 1500],
  [1000, 5000],
  [2000, 10_000],
];

test.skip(!ENABLED, 'opt-in: benchmarks/dashboard_graph.py sets E2E_GRAPH_PERF=1');
test.describe.configure({ mode: 'serial' });

const results: Record<string, unknown>[] = [];

async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            resolve();
          });
        });
      }),
  );
}

/** The intervals between animation frames while `action` runs (the first one is dropped). */
async function frames(page: Page, action: () => Promise<void>): Promise<number[]> {
  await page.evaluate(() => {
    const probe = (globalThis as ProbeGlobal).__probe;
    if (probe === undefined) return;
    probe.frames = [];
    probe.recording = true;
    let last = performance.now();
    const tick = (now: number) => {
      probe.frames.push(now - last);
      last = now;
      if (probe.recording) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await action();
  await settle(page);
  return page.evaluate(() => {
    const probe = (globalThis as ProbeGlobal).__probe;
    if (probe === undefined) return [];
    probe.recording = false;
    return probe.frames.slice(1);
  });
}

for (const [nodes, edges] of SIZES) {
  for (let trial = 1; trial <= REPEAT; trial += 1) {
    test(`graph of up to ${String(nodes)} nodes and ${String(edges)} links, trial ${String(trial)}`, async ({
      page,
    }) => {
      test.setTimeout(300_000);
      await page.addInitScript(() => {
        const probe: Probe = { longTasks: [], frames: [], recording: false };
        (globalThis as ProbeGlobal).__probe = probe;
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) probe.longTasks.push(entry.duration);
        }).observe({ type: 'longtask', buffered: true });
      });
      const responded = page.waitForResponse((response) => response.url().includes('/link-graph'));
      await page.goto(`/graph?job=${JOB}&nodes=${String(nodes)}&edges=${String(edges)}`);
      const response = await responded;
      const body = (await response.json()) as { nodes: unknown[]; edges: unknown[] };
      const timing = response.request().timing();
      const drawn = await page.waitForFunction(
        (count) =>
          document.querySelectorAll('[data-node]').length >= count ? performance.now() : false,
        body.nodes.length,
        { timeout: 120_000, polling: 'raf' },
      );
      const drawnMs = Number(await drawn.jsonValue());
      await settle(page);
      const load = await page.evaluate(() => {
        const probe = (globalThis as ProbeGlobal).__probe;
        return {
          longTasks: probe?.longTasks.slice() ?? [],
          heapBytes: (performance as MemoryPerformance).memory?.usedJSHeapSize ?? null,
          domElements: document.getElementsByTagName('*').length,
        };
      });
      // The graph's own drawing: the figure's other SVGs are the toolbar's icons.
      const graph = page.locator('figure svg').filter({ has: page.locator('[data-node]') });
      const box = await graph.boundingBox();
      if (box === null) throw new Error('the graph has no box');
      const centerX = box.x + box.width / 2;
      const centerY = box.y + box.height / 2;
      const pan = await frames(page, async () => {
        await page.mouse.move(centerX, centerY);
        await page.mouse.down();
        for (let step = 1; step <= 40; step += 1) {
          await page.mouse.move(centerX + step * 6, centerY + step * 3);
        }
        await page.mouse.up();
      });
      const zoom = await frames(page, async () => {
        await page.mouse.move(centerX, centerY);
        for (let step = 0; step < 25; step += 1) await page.mouse.wheel(0, -120);
      });
      results.push({
        requested: { nodes, edges },
        trial,
        drawn: { nodes: body.nodes.length, edges: body.edges.length },
        response: {
          responseStartMs: timing.responseStart,
          responseEndMs: timing.responseEnd,
          bytes: (await response.body()).length,
        },
        drawnMs,
        longTasksMs: load.longTasks,
        heapBytes: load.heapBytes,
        domElements: load.domElements,
        panFramesMs: pan,
        zoomFramesMs: zoom,
      });
    });
  }
}

test.afterAll(() => {
  if (OUTPUT !== '') writeFileSync(OUTPUT, `${JSON.stringify(results, null, 2)}\n`);
});
