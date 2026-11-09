/**
 * Screenshots of every page against the real stack, at desktop and phone widths, for a visual
 * review (layout, overflow, labels) and for the README. Opt-in: set E2E_SCREENSHOTS=1. Images go
 * to E2E_SCREENSHOTS_DIR (default test-results/screens/, git-ignored).
 *
 * Two kinds of stack:
 *
 * - `tests.dashboard_stack` (writes open): the first test crawls the demo site through the form
 *   (E2E_DEMO_SEED).
 * - A deployed, read-only stack (deploy/compose.yaml, seeded with
 *   `python -m app.scheduler.admin seed-demo`): set E2E_READ_ONLY=1, and the first test finds
 *   the seeded full crawl of the demo site instead.
 *
 * Every page must load without a Content-Security-Policy violation or an uncaught error: the
 * deployed proxy sends a strict CSP (frontend/nginx/security-headers.conf).
 */
import { expect, test, type Page } from '@playwright/test';

const DEMO_SEED = process.env.E2E_DEMO_SEED ?? '';
const READ_ONLY = process.env.E2E_READ_ONLY === '1';
const DIRECTORY = process.env.E2E_SCREENSHOTS_DIR ?? 'test-results/screens';
// Whole pages for a review; E2E_SCREENSHOTS_FULL_PAGE=0 for viewport-sized images (the README).
const FULL_PAGE = process.env.E2E_SCREENSHOTS_FULL_PAGE !== '0';

test.skip(process.env.E2E_SCREENSHOTS !== '1', 'screenshots are opt-in (E2E_SCREENSHOTS=1)');
test.describe.configure({ mode: 'serial' });

let jobId = '';
let problems: string[] = [];

test.beforeEach(({ page }) => {
  problems = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && /Content Security Policy/i.test(message.text())) {
      problems.push(`CSP: ${message.text()}`);
    }
  });
  page.on('pageerror', (error) => {
    problems.push(`page error: ${error.message}`);
  });
});

test.afterEach(() => {
  expect(problems).toEqual([]);
});

async function shoot(page: Page, name: string) {
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: `${DIRECTORY}/${name}.png`, fullPage: FULL_PAGE });
}

interface Summary {
  id: string;
  seed_url: string;
  max_pages: number;
  status: string;
}

test('the demo site crawl to show', async ({ page, request }) => {
  if (READ_ONLY) {
    const response = await request.get('/api/v1/crawls?limit=100');
    expect(response.ok()).toBe(true);
    const { items } = (await response.json()) as { items: Summary[] };
    // The seeded full crawl: seed "/", a page limit of 100 (app/scheduler/admin.py, DEMO_JOBS).
    const job = items.find(
      (item) =>
        item.status === 'completed' &&
        item.max_pages === 100 &&
        new URL(item.seed_url).pathname === '/',
    );
    expect(job, 'seed the demo jobs first (app.scheduler.admin seed-demo)').toBeDefined();
    jobId = job?.id ?? '';
    await page.goto('/crawls');
    await expect(page.getByText('This deployment is read-only')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Create crawl job' })).toHaveCount(0);
    await shoot(page, 'crawls-read-only');
    return;
  }
  await page.goto('/crawls');
  await page.getByLabel('Seed URL').fill(DEMO_SEED);
  await page.getByRole('button', { name: 'Create crawl job' }).click();
  await page.waitForURL(/\/crawls\/[0-9a-f-]{36}$/);
  jobId = new URL(page.url()).pathname.split('/').pop() ?? '';
  await shoot(page, 'job-detail-running');
  await expect(page.getByText('Fully indexed', { exact: true }).first()).toBeVisible({
    timeout: 120_000,
  });
});

for (const [width, height, suffix] of [
  [1440, 900, 'desktop'],
  [390, 844, 'phone'],
] as const) {
  test(`pages at ${suffix} width`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto('/');
    await expect(page.getByText('Newest job:')).toBeVisible();
    await shoot(page, `overview-${suffix}`);
    await page.goto('/crawls');
    await expect(page.getByRole('table', { name: 'Crawl jobs, newest first' })).toBeVisible();
    await shoot(page, `crawls-${suffix}`);
    await page.goto(`/crawls/${jobId}`);
    await expect(page.getByText(/Final state/)).toBeVisible();
    await shoot(page, `job-detail-${suffix}`);
    await page.goto(`/search?job=${jobId}&q=politely`);
    await expect(page.getByRole('region', { name: 'Search results' })).toBeVisible();
    await shoot(page, `search-${suffix}`);
    await page.goto(`/analysis?job=${jobId}`);
    await expect(page.getByRole('region', { name: 'Crawl statistics' })).toBeVisible();
    await shoot(page, `analysis-${suffix}`);
    await page.goto(`/analysis?job=${jobId}&tab=links`);
    await expect(
      page.getByRole('table', { name: 'Stored links with their classification' }),
    ).toBeVisible();
    await shoot(page, `links-${suffix}`);
    await page.goto(`/analysis?job=${jobId}&tab=broken`);
    await expect(page.getByRole('table', { name: 'Confirmed broken links' })).toBeVisible();
    await shoot(page, `broken-${suffix}`);
    await page.goto(`/graph?job=${jobId}`);
    await expect(page.getByRole('figure')).toBeVisible();
    await page
      .getByRole('list', { name: 'Graph nodes' })
      .getByRole('button', { name: /about\.html/ })
      .click();
    await shoot(page, `graph-${suffix}`);
  });
}
