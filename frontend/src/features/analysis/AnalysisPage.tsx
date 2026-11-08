import { useSearchParams } from 'react-router';

import { tabPanelProps } from '../../components/a11y';
import { ErrorState, LoadingState } from '../../components/feedback';
import { Tabs, type TabItem } from '../../components/Tabs';
import { JobGate } from '../crawls/JobGate';
import { useLiveCrawlJob } from '../crawls/hooks';
import { BrokenLinksPanel } from './BrokenLinksPanel';
import { LinksPanel } from './LinksPanel';
import { PagesPanel } from './PagesPanel';
import { StatsPanel } from './StatsPanel';

type TabKey = 'overview' | 'pages' | 'links' | 'broken';

const TABS: readonly TabItem<TabKey>[] = [
  { key: 'overview', label: 'Statistics' },
  { key: 'pages', label: 'Pages' },
  { key: 'links', label: 'Links' },
  { key: 'broken', label: 'Broken links' },
];

function parseTab(raw: string | null): TabKey {
  return TABS.find((tab) => tab.key === raw)?.key ?? 'overview';
}

export function AnalysisPage() {
  return <JobGate>{(jobId) => <AnalysisView jobId={jobId} />}</JobGate>;
}

function AnalysisView({ jobId }: { jobId: string }) {
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get('tab'));
  // The job's own record: its depth limit (page filter) and whether it still runs.
  const { query: job } = useLiveCrawlJob(jobId, 2);

  function href(next: Record<string, string>): string {
    const query = new URLSearchParams({ job: jobId, ...next });
    return `/analysis?${query.toString()}`;
  }

  function select(next: TabKey) {
    setParams((current) => {
      const updated = new URLSearchParams(current);
      updated.set('tab', next);
      return updated;
    });
  }

  if (job.isPending) return <LoadingState label="Loading the crawl job…" />;
  if (job.isError && job.data === undefined) {
    return (
      <ErrorState error={job.error} onRetry={() => void job.refetch()} retrying={job.isFetching} />
    );
  }

  return (
    <div className="space-y-5">
      <Tabs
        items={TABS}
        selected={tab}
        onSelect={select}
        label="Analysis views"
        idPrefix="analysis"
      />
      <div {...tabPanelProps('analysis', tab)} className="outline-none">
        {tab === 'overview' && (
          <StatsPanel
            jobId={jobId}
            linksHref={(linkClass) => href({ tab: 'links', class: linkClass })}
          />
        )}
        {tab === 'pages' && (
          <PagesPanel
            jobId={jobId}
            maxDepth={job.data.max_depth}
            linksOf={(pageId) => href({ tab: 'links', source: pageId })}
          />
        )}
        {tab === 'links' && <LinksPanel jobId={jobId} />}
        {tab === 'broken' && (
          <BrokenLinksPanel
            jobId={jobId}
            unverifiedHref={href({ tab: 'links', verified: 'false' })}
          />
        )}
      </div>
    </div>
  );
}
