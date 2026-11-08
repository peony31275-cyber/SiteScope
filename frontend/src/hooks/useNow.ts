import { useSyncExternalStore } from 'react';

/**
 * The current time, for relative times ("3 minutes ago"). One shared timer per granularity,
 * running only while a component uses it: a table of rows asks for a coarse clock (30 s), so
 * its rows do not all re-render every second.
 */
interface Clock {
  current: number;
  listeners: Set<() => void>;
  timer: ReturnType<typeof setInterval> | undefined;
  subscribe: (listener: () => void) => () => void;
  snapshot: () => number;
}

const clocks = new Map<number, Clock>();

function clockOf(granularityMs: number): Clock {
  const existing = clocks.get(granularityMs);
  if (existing !== undefined) return existing;
  const clock: Clock = {
    current: Date.now(),
    listeners: new Set(),
    timer: undefined,
    subscribe: (listener) => {
      clock.current = Date.now();
      clock.listeners.add(listener);
      clock.timer ??= setInterval(() => {
        clock.current = Date.now();
        for (const notify of clock.listeners) notify();
      }, granularityMs);
      return () => {
        clock.listeners.delete(listener);
        if (clock.listeners.size === 0 && clock.timer !== undefined) {
          clearInterval(clock.timer);
          clock.timer = undefined;
        }
      };
    },
    snapshot: () => clock.current,
  };
  clocks.set(granularityMs, clock);
  return clock;
}

export function useNow(granularityMs = 1000): number {
  const clock = clockOf(granularityMs);
  return useSyncExternalStore(clock.subscribe, clock.snapshot);
}
