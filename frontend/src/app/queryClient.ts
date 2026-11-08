import { QueryClient } from '@tanstack/react-query';

import { isTransient } from '../lib/api/errors';

/** Retries: only failures that may go away on their own (the API or PostgreSQL/Redis briefly
 * unreachable), at most twice, with exponential backoff and jitter. A 404, a validation error
 * or a query time limit is shown at once. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => isTransient(error) && failureCount < 2,
        retryDelay: (attempt) => Math.min(8000, 1000 * 2 ** attempt) * (0.5 + Math.random() / 2),
        // Running jobs are polled explicitly; finished data does not change by itself.
        refetchOnWindowFocus: false,
        staleTime: 5_000,
      },
      mutations: { retry: false },
    },
  });
}
