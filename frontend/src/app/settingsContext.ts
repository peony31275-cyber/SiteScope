/**
 * Viewer settings: the auto-refresh interval for running jobs. Kept in the browser
 * (localStorage, per viewer); everything works the same when storage is unavailable.
 */
import { createContext, useContext } from 'react';

export const POLL_CHOICES = [2000, 3000, 5000, 10_000, 30_000] as const;
const STORAGE_KEY = 'sitescope.pollIntervalMs';

/** The deployment's default (VITE_POLL_INTERVAL_MS), within 1-60 s; 3 s otherwise. */
export function defaultPollInterval(
  raw: string | undefined = import.meta.env.VITE_POLL_INTERVAL_MS,
): number {
  const value = Number(raw);
  return Number.isFinite(value) && value >= 1000 && value <= 60_000 ? Math.round(value) : 3000;
}

/** The viewer's stored choice: a number, null for "off", undefined when there is none. */
export function readStoredPollInterval(): number | null | undefined {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) return undefined;
    if (stored === 'off') return null;
    const value = Number(stored);
    return Number.isFinite(value) && value >= 1000 && value <= 60_000 ? value : undefined;
  } catch {
    return undefined;
  }
}

export function storePollInterval(value: number | null): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, value === null ? 'off' : String(value));
  } catch {
    // Storage unavailable (private window, blocked site data): the choice lasts for this visit.
  }
}

export interface SettingsValue {
  /** Milliseconds between refreshes of running jobs, or null when auto-refresh is off. */
  pollIntervalMs: number | null;
  setPollIntervalMs: (value: number | null) => void;
}

export const SettingsContext = createContext<SettingsValue | null>(null);

export function useSettings(): SettingsValue {
  const value = useContext(SettingsContext);
  if (value === null) throw new Error('useSettings must be used inside SettingsProvider');
  return value;
}
