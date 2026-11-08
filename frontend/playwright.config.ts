import { defineConfig } from '@playwright/test';

/**
 * The dashboard's end-to-end run against the real stack. It does not start anything itself:
 * `python -m tests.dashboard_stack --e2e` (in backend/) starts every process on a disposable
 * database and runs this suite with E2E_BASE_URL, E2E_DEMO_SEED and E2E_SLOW_SEED set.
 *
 * The browser is the installed Microsoft Edge (channel `msedge`), so no browser download is
 * needed on Windows; set E2E_BROWSER_CHANNEL to use another installed Chromium (`chrome`).
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  outputDir: 'test-results',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:4173',
    channel: process.env.E2E_BROWSER_CHANNEL ?? 'msedge',
    headless: true,
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
