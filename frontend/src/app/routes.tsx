import type { RouteObject } from 'react-router';

import { LoadingState } from '../components/feedback';
import { AppShell } from './AppShell';
import type { RouteHandle } from './navigation';
import { NotFoundPage, RouteError } from './RouteError';

function handle(value: RouteHandle): RouteHandle {
  return value;
}

/** Every page is its own chunk, loaded when it is first visited: the charts and the graph
 * code are not part of the initial download. */
export const routes: RouteObject[] = [
  {
    path: '/',
    Component: AppShell,
    ErrorBoundary: RouteError,
    HydrateFallback: () => <LoadingState label="Loading the dashboard…" className="min-h-screen" />,
    children: [
      {
        index: true,
        handle: handle({ title: 'Overview' }),
        lazy: async () => ({
          Component: (await import('../features/overview/OverviewPage')).OverviewPage,
        }),
      },
      {
        path: 'crawls',
        handle: handle({ title: 'Crawl jobs' }),
        lazy: async () => ({
          Component: (await import('../features/crawls/CrawlJobsPage')).CrawlJobsPage,
        }),
      },
      {
        path: 'crawls/:jobId',
        handle: handle({ title: 'Crawl job' }),
        lazy: async () => ({
          Component: (await import('../features/crawls/JobDetailPage')).JobDetailPage,
        }),
      },
      {
        path: 'search',
        handle: handle({ title: 'Search explorer', jobScoped: true }),
        lazy: async () => ({
          Component: (await import('../features/search/SearchPage')).SearchPage,
        }),
      },
      {
        path: 'analysis',
        handle: handle({ title: 'Site analysis', jobScoped: true }),
        lazy: async () => ({
          Component: (await import('../features/analysis/AnalysisPage')).AnalysisPage,
        }),
      },
      {
        path: 'graph',
        handle: handle({ title: 'Link explorer', jobScoped: true }),
        lazy: async () => ({
          Component: (await import('../features/graph/LinkExplorerPage')).LinkExplorerPage,
        }),
      },
      { path: '*', handle: handle({ title: 'Not found' }), Component: NotFoundPage },
    ],
  },
];
