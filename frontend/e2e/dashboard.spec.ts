/**
 * The dashboard against the real SiteScope stack (no mocks): the API, PostgreSQL, Redis, the
 * scheduler, crawler workers, the index dispatcher and indexers, crawling the demo site on
 * 127.0.0.1. Started by `python -m tests.dashboard_stack --e2e` (backend/); see
 * playwright.config.ts.
 *
 * The demo site's known structure (demo-site/README.md): 14 URLs admitted at depth 3, 12 pages
 * stored, 11 indexed (team.html is noindex), 15 URLs discovered, 26 links (24 internal), one 404
 * (/about.html links to /missing.html), /private/notes.html blocked by robots.txt.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const DEMO_SEED = process.env.E2E_DEMO_SEED ?? '';
const SLOW_SEED = process.env.E2E_SLOW_SEED ?? '';

test.describe.configure({ mode: 'serial' });

let demoJobId = '';

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`/api/v1${path}`);
  expect(response.ok(), `GET ${path}: ${String(response.status())}`).toBe(true);
  return (await response.json()) as T;
}

interface JobDetail {
  id: string;
  status: string;
  admitted_count: number;
  progress: { pages_stored: number; urls_total: number };
  indexing: { status: string; documents_total: number; documents_by_status: { indexed: number } };
}

async function createJob(page: Page, seed: string, depth: string, pages: string): Promise<string> {
  await page.goto('/crawls');
  const seedField = page.getByLabel('Seed URL');
  await seedField.fill(seed);
  await page.getByLabel('Maximum BFS depth').fill(depth);
  await page.getByLabel('Page limit').fill(pages);
  await page.getByRole('button', { name: 'Create crawl job' }).click();
  await page.waitForURL(/\/crawls\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split('/').pop() ?? '';
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  return id;
}

test('the dashboard reaches the API, PostgreSQL and Redis', async ({ page }) => {
  expect(DEMO_SEED, 'E2E_DEMO_SEED is set by tests.dashboard_stack').not.toBe('');
  await page.goto('/');
  await expect(page.getByText('API online')).toBeVisible();
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'All crawl jobs' })).toBeVisible();
});

test('a crawl job created in the form is crawled and indexed, and the page follows it live', async ({
  page,
  request,
}) => {
  await page.goto('/crawls');
  // The approved targets come from the server's crawl policy.
  await expect(page.getByRole('button', { name: DEMO_SEED })).toBeVisible();
  demoJobId = await createJob(page, DEMO_SEED, '3', '100');
  await expect(page.getByText('Crawl job created')).toBeVisible();
  const header = page.getByRole('region', { name: 'Crawl job' });
  await expect(header).toContainText(demoJobId);

  // The page refreshes by itself until the crawl and its indexing are both over.
  await expect(header.getByText('Completed', { exact: true })).toBeVisible({ timeout: 120_000 });
  await expect(header.getByText('Fully indexed', { exact: true })).toBeVisible({
    timeout: 120_000,
  });
  await expect(page.getByText(/Final state: the crawl and its indexing are over/)).toBeVisible();

  // What the page shows is what the API says.
  const job = await apiJson<JobDetail>(request, `/crawls/${demoJobId}`);
  expect([job.status, job.indexing.status]).toEqual(['completed', 'indexed']);
  expect([
    job.admitted_count,
    job.progress.pages_stored,
    job.indexing.documents_by_status.indexed,
  ]).toEqual([14, 12, 11]);
  const tile = (label: string) =>
    page.locator('dl > div', { has: page.getByText(label, { exact: true }) });
  await expect(tile('URLs admitted')).toContainText(String(job.admitted_count));
  await expect(tile('Pages stored')).toContainText(String(job.progress.pages_stored));
  await expect(tile('Indexed pages')).toContainText(
    `of ${String(job.indexing.documents_total)} to index`,
  );
  await expect(
    header.getByText('Crawl completed · fully indexed (11 of 11 pages indexed).'),
  ).toBeVisible();
});

test('search finds indexed pages, marks the matched words, and never shows noindex pages', async ({
  page,
}) => {
  await page.goto(`/crawls/${demoJobId}`);
  await page.getByRole('link', { name: 'Search', exact: true }).click();
  // The job page's header is a region labelled "Crawl job" too: until the search page replaces
  // it, getByLabel finds that region, not the selector (a CI failure, "Not an input element").
  await expect(page.getByRole('combobox', { name: 'Crawl job', exact: true })).toHaveValue(
    demoJobId,
  );
  await page.getByLabel('Search query').fill('fencing');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  const results = page.getByRole('region', { name: 'Search results' });
  await expect(
    results.getByRole('link', { name: /Architecture of a polite crawler/ }),
  ).toBeVisible();
  await expect(results.locator('mark').first()).toBeVisible();
  await expect(results).toContainText('1 result for “fencing”');

  await page.getByLabel('Search query').fill('noindex');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.getByText(/No indexed page matches/)).toBeVisible();
});

test('site analysis reports the statistics, the one confirmed broken link, and unverified links as unverified', async ({
  page,
  request,
}) => {
  await page.goto(`/analysis?job=${demoJobId}`);
  const crawl = page.getByRole('region', { name: 'Crawl statistics' });
  const stats = await apiJson<{
    crawl: { urls_discovered: number; urls_admitted: number; pages_stored: number };
  }>(request, `/crawls/${demoJobId}/stats`);
  expect([
    stats.crawl.urls_discovered,
    stats.crawl.urls_admitted,
    stats.crawl.pages_stored,
  ]).toEqual([15, 14, 12]);
  const tile = (label: string) =>
    crawl.locator('dl > div', { has: page.getByText(label, { exact: true }) });
  await expect(tile('URLs discovered')).toContainText('15');
  await expect(tile('URLs admitted')).toContainText('14');

  await page.getByRole('tab', { name: 'Broken links', exact: true }).click();
  const broken = page.getByRole('table', { name: 'Confirmed broken links' });
  await expect(broken.getByRole('row')).toHaveCount(2);
  await expect(broken).toContainText('/missing.html');
  await expect(broken).toContainText('404 Not Found');
  await expect(broken).toContainText('2024 roadmap');

  await page.getByRole('tab', { name: 'Links', exact: true }).click();
  await page.getByLabel('Classification').selectOption('robots_blocked');
  const links = page.getByRole('table', { name: 'Stored links with their classification' });
  await expect(links).toContainText('/private/notes.html');
  await expect(links).toContainText('Robots blocked');
  await expect(links).toContainText('unverified');
  await expect(links).not.toContainText('Broken');
});

test('the link explorer draws the graph, explains a selected node, and warns when it is cut', async ({
  page,
}) => {
  await page.goto(`/graph?job=${demoJobId}`);
  await expect(page.getByRole('figure', { name: /15 nodes and 24 links shown/ })).toBeVisible();
  await expect(page.locator('[data-node]')).toHaveCount(15);
  await page
    .getByRole('list', { name: 'Graph nodes' })
    .getByRole('button', { name: /\/missing\.html/ })
    .click();
  const details = page.getByRole('region', { name: 'Node details' });
  await expect(details).toContainText('Admitted URL without a page');
  await expect(details).toContainText('404');

  await page.getByLabel('Most nodes').fill('5');
  await page.getByRole('button', { name: 'Apply limits' }).click();
  await expect(page.getByText('Partial graph: cut at the limits')).toBeVisible();
  await expect(page.getByText(/Showing 5 of 15 nodes/)).toBeVisible();
});

test('a running crawl is cancelled through the confirmation dialog, as the server confirms it', async ({
  page,
  request,
}) => {
  expect(SLOW_SEED, 'E2E_SLOW_SEED is set by tests.dashboard_stack').not.toBe('');
  const jobId = await createJob(page, SLOW_SEED, '2', '100');
  const header = page.getByRole('region', { name: 'Crawl job' });
  await expect(header.getByText('Running', { exact: true })).toBeVisible({ timeout: 60_000 });

  await page.getByRole('button', { name: 'Cancel crawl' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: 'Keep crawling' })).toBeFocused();
  await dialog.getByRole('button', { name: 'Cancel the crawl' }).click();
  await expect(page.getByText(/^(Cancellation requested|Crawl cancelled)$/)).toBeVisible();
  await expect(header.getByText('Cancelled', { exact: true })).toBeVisible({ timeout: 60_000 });

  const job = await apiJson<JobDetail>(request, `/crawls/${jobId}`);
  expect(job.status).toBe('cancelled');
  await expect(page.getByRole('button', { name: 'Cancel crawl' })).toHaveCount(0);
});

test('no page scrolls sideways, on a desktop or a phone', async ({ page }) => {
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of [
      '/',
      '/crawls',
      `/crawls/${demoJobId}`,
      `/search?job=${demoJobId}&q=politely`,
      `/analysis?job=${demoJobId}`,
      `/analysis?job=${demoJobId}&tab=links`,
      `/analysis?job=${demoJobId}&tab=broken`,
      `/graph?job=${demoJobId}`,
    ]) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `${path} at ${String(width)} px`).toBeLessThanOrEqual(0);
    }
  }
});

test('an unknown job and an unknown page are explained', async ({ page }) => {
  await page.goto('/crawls/00000000-0000-4000-8000-000000000000');
  await expect(page.getByText('Crawl job not found')).toBeVisible();
  await page.goto('/no/such/page');
  await expect(page.getByText('Page not found')).toBeVisible();
});
