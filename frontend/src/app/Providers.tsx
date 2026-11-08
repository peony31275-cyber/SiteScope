import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { SettingsProvider } from './settings';
import { ToastProvider } from './toast';

interface ProvidersProps {
  client: QueryClient;
  children: ReactNode;
  /** Tests fix the auto-refresh interval instead of reading the browser's choice. */
  pollIntervalMs?: number | null;
}

export function Providers({ client, children, pollIntervalMs }: ProvidersProps) {
  return (
    <QueryClientProvider client={client}>
      <SettingsProvider initialPollIntervalMs={pollIntervalMs}>
        <ToastProvider>{children}</ToastProvider>
      </SettingsProvider>
    </QueryClientProvider>
  );
}
