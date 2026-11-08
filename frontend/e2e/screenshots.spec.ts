/**
 * Screenshots of every page against the real stack, at desktop and phone widths, for a visual
 * review (layout, overflow, labels). Opt-in: set E2E_SCREENSHOTS=1. The images are written to
 * test-results/screens/ (git-ignored).
 */
import { expect, test, type Page } from '@playwright/test';

const DEMO_SEED = process.env.E2E_DEMO_SEED ?? '';

test.skip(process.env.E2E_SCREENSHOTS !== '1', 'screenshots are opt-in (E2E_SCREENSHOTS=1)');
test.describe.configure({ mode: 'serial' });

let jobId = '';

async function shoot(page: Page, name: string) {
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: `test-results/screens/${name}.png`, fullPage: true });
}

test('crawl the demo site for the screenshots', async ({ page }) => {
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
