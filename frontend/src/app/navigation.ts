import {
  ChartColumn,
  LayoutDashboard,
  ListChecks,
  Network,
  Search,
  type LucideIcon,
} from 'lucide-react';
import type { UIMatch } from 'react-router';

/** What each route tells the shell: its title, and whether it works on one crawl job (the
 * header then shows the job selector). */
export interface RouteHandle {
  title: string;
  jobScoped?: boolean;
}

function isRouteHandle(value: unknown): value is RouteHandle {
  return (
    typeof value === 'object' &&
    value !== null &&
    'title' in value &&
    typeof value.title === 'string'
  );
}

/** The handle of the deepest matched route that has one. */
export function currentHandle(matches: readonly UIMatch[]): RouteHandle {
  for (let index = matches.length - 1; index >= 0; index -= 1) {
    const handle: unknown = matches[index]?.handle;
    if (isRouteHandle(handle)) return handle;
  }
  return { title: 'SiteScope' };
}

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Carries the selected job along, so switching views keeps the job. */
  jobScoped: boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, jobScoped: false },
  { to: '/crawls', label: 'Crawl Jobs', icon: ListChecks, jobScoped: false },
  { to: '/search', label: 'Search Explorer', icon: Search, jobScoped: true },
  { to: '/analysis', label: 'Site Analysis', icon: ChartColumn, jobScoped: true },
  { to: '/graph', label: 'Link Explorer', icon: Network, jobScoped: true },
];
