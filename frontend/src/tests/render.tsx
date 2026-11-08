import { QueryClient } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';

import { Providers } from '../app/Providers';
import { routes } from '../app/routes';

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
