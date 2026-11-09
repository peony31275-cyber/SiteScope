import { QueryClient } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { beforeAll } from 'vitest';

import { Providers } from '../app/Providers';
import { routes } from '../app/routes';

/**
 * Load every page's chunk (routes.tsx: each page is loaded lazily) before the tests of a file
 * that renders pages. A file's first import of a page, charts included, is a cold cost: under a
 * full parallel run it once exceeded Testing Library's 3 s wait in the first AnalysisPage test
 * (measured: 1.4 s alone, 3.9 s for that test in the full run). Loaded here, under the hook's
 * own time limit, no findBy* wait pays for it; react-router's own lazy() calls then find the
 * modules loaded.
 */
async function preloadPages(): Promise<void> {
  const loads: Promise<unknown>[] = [];
  for (const route of routes.flatMap((parent) => parent.children ?? [])) {
    if (typeof route.lazy === 'function') loads.push(route.lazy());
  }
  await Promise.all(loads);
}

beforeAll(preloadPages, 30_000);

interface RenderOptions {
  /** Auto-refresh interval; off unless a test is about polling. */
  pollIntervalMs?: number | null;
  /** Let TanStack Query retry transient failures as in production (off by default). */
  retry?: boolean;
  /** For fake timers: user-event waits with them. */
  advanceTimers?: (delay: number) => unknown;
}

/** The whole dashboard (router, providers, lazy pages) at `path`, as the browser would show it. */
export function renderApp(
  path: string,
  { pollIntervalMs = null, retry = false, advanceTimers }: RenderOptions = {},
) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: retry ? 2 : false,
        retryDelay: 10,
        refetchOnWindowFocus: false,
        staleTime: 5_000,
      },
      mutations: { retry: false },
    },
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const user = userEvent.setup(
    advanceTimers === undefined
      ? {}
      : {
          advanceTimers: async (delay: number) => {
            await advanceTimers(delay);
          },
        },
  );
  const result = render(
    <Providers client={client} pollIntervalMs={pollIntervalMs}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return { ...result, client, router, user };
}
