import { useState } from 'react';

/**
 * A walk through a cursor-paged listing. The server's cursors are opaque: they are passed back
 * unchanged, never parsed or built, and there is no "page N" to jump to. Going back re-reads
 * the cursor that led to the previous page. A change of `resetKey` (another job, other filters,
 * another page size) starts a new walk from the first page.
 */
export interface CursorPager {
  /** The cursor of the page to show; null for the first page. */
  cursor: string | null;
  /** 1-based position in this walk. */
  page: number;
  hasPrevious: boolean;
  next: (nextCursor: string) => void;
  previous: () => void;
}

export function useCursorPager(resetKey: string): CursorPager {
  const [walk, setWalk] = useState<{ key: string; cursors: (string | null)[] }>({
    key: resetKey,
    cursors: [null],
  });
  let cursors = walk.cursors;
  if (walk.key !== resetKey) {
    cursors = [null];
    setWalk({ key: resetKey, cursors });
  }
  return {
    cursor: cursors[cursors.length - 1] ?? null,
    page: cursors.length,
    hasPrevious: cursors.length > 1,
    next: (nextCursor) => {
      setWalk((current) => ({ ...current, cursors: [...current.cursors, nextCursor] }));
    },
    previous: () => {
      setWalk((current) =>
        current.cursors.length > 1
          ? { ...current, cursors: current.cursors.slice(0, -1) }
          : current,
      );
    },
  };
}
