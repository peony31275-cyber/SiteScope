/**
 * Listing filters, kept in the address (`?class=broken&verified=false`) so a filtered view can
 * be shared. Values are parsed strictly: anything unknown means "no filter", and is never sent.
 */
import type { LinkClass, PageIndexStatus } from '../../lib/api/types';
import { isUuid } from '../../lib/urls';
import { LINK_CLASSES } from './linkClasses';

export type TriState = 'any' | 'true' | 'false';

export function parseTriState(raw: string | null): TriState {
  return raw === 'true' || raw === 'false' ? raw : 'any';
}

export function triStateValue(value: TriState): boolean | undefined {
  return value === 'any' ? undefined : value === 'true';
}

export function parseLinkClass(raw: string | null): LinkClass | null {
  return raw !== null && Object.hasOwn(LINK_CLASSES, raw) ? (raw as LinkClass) : null;
}

export const PAGE_INDEX_STATUSES: readonly PageIndexStatus[] = [
  'pending',
  'queued',
  'indexed',
  'failed',
  'noindex',
];

export function parseIndexStatus(raw: string | null): PageIndexStatus | null {
  return PAGE_INDEX_STATUSES.find((status) => status === raw) ?? null;
}

/** A BFS depth filter: a whole number from 0 to 1000 (the API's bound), or null. */
export function parseDepth(raw: string | null): number | null {
  if (raw === null || !/^\d{1,4}$/.test(raw)) return null;
  const value = Number(raw);
  return value <= 1000 ? value : null;
}

export function parseSourcePage(raw: string | null): string | null {
  return raw !== null && isUuid(raw) ? raw.toLowerCase() : null;
}

/** Items per page of the analysis listings (the API allows 1-200, default 50). */
export const ANALYSIS_PAGE_SIZE = 50;
