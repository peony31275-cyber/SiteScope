/**
 * Search pages by number (`page`, `page_size`), unlike the cursor-paged listings: results are
 * ranked, and the server serves them up to offset 1000 (`MAX_OFFSET`); a page with
 * `(page - 1) * page_size` above it is refused (`page_out_of_range`).
 */
export const SEARCH_MAX_OFFSET = 1000;
export const SEARCH_PAGE_SIZES = [10, 20, 50] as const;
export const SEARCH_MAX_QUERY_LENGTH = 1024; // characters of `q` before normalization

/** The deepest page the server serves for this page size. */
export function lastServablePage(pageSize: number): number {
  return Math.floor(SEARCH_MAX_OFFSET / pageSize) + 1;
}

/** Pages to offer: as many as the results fill, but never one the server refuses. */
export function searchPageCount(total: number, pageSize: number): number {
  return Math.min(Math.ceil(total / pageSize), lastServablePage(pageSize));
}

/** A `page` URL parameter as a page number, or 1. */
export function parsePage(raw: string | null): number {
  const value = Number(raw);
  return Number.isInteger(value) && value >= 1 ? value : 1;
}

export function parsePageSize(raw: string | null): number {
  const value = Number(raw);
  return (SEARCH_PAGE_SIZES as readonly number[]).includes(value) ? value : 10;
}
