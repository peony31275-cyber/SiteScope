import { useCallback, useState } from 'react';

/**
 * Pauses polling that brings nothing new. It counts consecutive successful reads whose
 * signature did not change; after `limit` of them in a row, `stalled` is true and the caller
 * stops polling. Reads happen only while a page is visible and polled, so a hidden tab never
 * counts towards a pause (a wall-clock timer would: review finding of Phase 9). New data, or
 * `resume`, restarts the count.
 */
interface Watch {
  updatedAt: number;
  signature: string | null;
  unchanged: number;
  limit: number;
}

export interface StallWatch {
  stalled: boolean;
  /** Record the latest read: call it while rendering, with the query's data and its time. */
  observe: (signature: string | null, updatedAt: number, limit: number) => void;
  resume: () => void;
}

const INITIAL: Watch = {
  updatedAt: 0,
  signature: null,
  unchanged: 0,
  limit: Number.POSITIVE_INFINITY,
};

export function useStallWatch(): StallWatch {
  const [watch, setWatch] = useState<Watch>(INITIAL);

  function observe(signature: string | null, updatedAt: number, limit: number): void {
    if (signature === null || updatedAt === watch.updatedAt) return;
    // Adjusting state while rendering, guarded by the read's time: React renders again at once
    // with the new count, and a read is counted exactly once.
    setWatch({
      updatedAt,
      signature,
      limit,
      unchanged: signature === watch.signature ? watch.unchanged + 1 : 0,
    });
  }

  const resume = useCallback(() => {
    setWatch((current) => ({ ...current, unchanged: 0 }));
  }, []);

  return { stalled: watch.unchanged >= watch.limit, observe, resume };
}

/** How many unchanged reads in a row pause polling: about `windowMs` worth of reads at
 * `intervalMs`, and never fewer than 10, so a slow interval still gets several chances. */
export function stallReads(windowMs: number, intervalMs: number | false): number {
  if (intervalMs === false) return Number.POSITIVE_INFINITY;
  return Math.max(10, Math.ceil(windowMs / intervalMs));
}
