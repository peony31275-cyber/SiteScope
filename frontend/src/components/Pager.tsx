import { ChevronLeft, ChevronRight } from 'lucide-react';

import { pageNumbers } from './a11y';
import { Button } from './Button';
import { Spinner } from './Spinner';

interface CursorPagerProps {
  /** 1-based position in this walk through the listing (not a server offset). */
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
  loading?: boolean;
  label: string;
}

/**
 * Previous / next for a cursor-paged listing. The server returns an opaque `next_cursor`; the
 * dashboard keeps the cursors it has followed to go back. There is no page count and no jump
 * to page N: cursors are not offsets.
 */
export function CursorPager({
  page,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  loading = false,
  label,
}: CursorPagerProps) {
  if (!hasPrevious && !hasNext) return null;
  return (
    <nav aria-label={label} className="flex items-center justify-end gap-2 pt-3">
      {loading && <Spinner className="text-ink-3" size={14} />}
      <span className="text-xs text-ink-3 tabular-nums">Page {page}</span>
      <Button
        size="sm"
        onClick={onPrevious}
        disabled={!hasPrevious || loading}
        icon={<ChevronLeft aria-hidden="true" className="size-3.5" />}
      >
        Previous
      </Button>
      <Button size="sm" onClick={onNext} disabled={!hasNext || loading}>
        Next
        <ChevronRight aria-hidden="true" className="size-3.5" />
      </Button>
    </nav>
  );
}

interface PageNavProps {
  page: number;
  pageCount: number;
  onPage: (page: number) => void;
  loading?: boolean;
  label: string;
}

/** Numbered pages, for search (which pages by number up to the server's deepest offset). */
export function PageNav({ page, pageCount, onPage, loading = false, label }: PageNavProps) {
  if (pageCount <= 1) return null;
  return (
    <nav aria-label={label} className="flex flex-wrap items-center justify-center gap-1 pt-4">
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          onPage(page - 1);
        }}
        disabled={page <= 1 || loading}
        icon={<ChevronLeft aria-hidden="true" className="size-3.5" />}
      >
        Previous
      </Button>
      {pageNumbers(page, pageCount).map((item, index) =>
        item === null ? (
          <span key={`gap-${String(index)}`} aria-hidden="true" className="px-1 text-ink-3">
            …
          </span>
        ) : (
          <Button
            key={item}
            size="sm"
            variant={item === page ? 'primary' : 'ghost'}
            aria-current={item === page ? 'page' : undefined}
            aria-label={`Page ${String(item)}`}
            onClick={() => {
              onPage(item);
            }}
            disabled={loading}
            className="min-w-8 tabular-nums"
          >
            {item}
          </Button>
        ),
      )}
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          onPage(page + 1);
        }}
        disabled={page >= pageCount || loading}
      >
        Next
        <ChevronRight aria-hidden="true" className="size-3.5" />
      </Button>
    </nav>
  );
}
