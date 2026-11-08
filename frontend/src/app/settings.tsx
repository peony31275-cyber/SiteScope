import { useMemo, useState, type ReactNode } from 'react';

import {
  defaultPollInterval,
  readStoredPollInterval,
  SettingsContext,
  storePollInterval,
} from './settingsContext';

interface SettingsProviderProps {
  children: ReactNode;
  /** Tests fix the interval; otherwise the viewer's stored choice, or the deployment default. */
  initialPollIntervalMs?: number | null;
}

export function SettingsProvider({ children, initialPollIntervalMs }: SettingsProviderProps) {
  const [pollIntervalMs, setPoll] = useState<number | null>(() => {
    if (initialPollIntervalMs !== undefined) return initialPollIntervalMs;
    const stored = readStoredPollInterval();
    return stored === undefined ? defaultPollInterval() : stored;
  });
  const value = useMemo(
    () => ({
      pollIntervalMs,
      setPollIntervalMs: (next: number | null) => {
        setPoll(next);
        storePollInterval(next);
      },
    }),
    [pollIntervalMs],
  );
  return <SettingsContext value={value}>{children}</SettingsContext>;
}
